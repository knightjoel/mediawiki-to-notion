#
# prepare-mw-dump.py
#
# Parses a MediaWiki XML dump, iterating over each page, doing some slight
# reformatting and cleanup of the wikitext, and outputting each page as
# Markdown via pandoc.
#

import base64
import os
import os.path
import re
import sys
import xml.etree.ElementTree as ET

import pandoc


class XmlParser:

    def __init__(self, xmlfile, outdir, *, custom_prepare=None):
        self._custom_prepare = custom_prepare
        self._ns = {}
        self._outdir = outdir
        self._xmlfile = xmlfile

    def parse(self):
        for event, element in ET.iterparse(self._xmlfile, events=('start', 'end')):
            tag_name = element.tag.rsplit("}", 1)[-1].strip()

            if event == "end":
                if tag_name == 'namespace':
                    key = element.get('key')
                    if key == '0':
                        self._ns[key] = 'Main'
                    else:
                        self._ns[key] = element.text
                elif tag_name == 'page':
                    # This is the MediaWiki namespace.
                    ns_id = element.find('{*}ns').text
                    ns_name = self._ns_name_from_id(ns_id)

                    if ns_id == self._ns_id_from_name('Main'):
                        parser = WikitextParser(element, ns_name=ns_name,
                                                custom_prepare=self._custom_prepare)
                    elif ns_id == self._ns_id_from_name('File'):
                        parser = FileParser(element, ns_name=ns_name,
                                            custom_prepare=self._custom_prepare)
                    elif ns_id == self._ns_id_from_name('Category'):
                        parser = WikitextParser(element, ns_name=ns_name,
                                                custom_prepare=self._custom_prepare)
                    else:
                        # Use this parser just to glean some basics about
                        # the element.
                        parser = WikitextParser(element, ns_name=ns_name)
                        print("Skipping page '{}' because its namespace '{}'"
                              " is implicitly ignored".format(parser.title,
                                                              ns_name))
                        continue

                    print("Processing '{}' in namespace '{}'"
                          .format(parser.title,
                                  self._ns_name_from_id(ns_id)))
                    parser.prepare()
                    parser.save(self._outdir)

                    element.clear()

    def _ns_id_from_name(self, ns_name):
        for item in self._ns.items():
            if item[1] == ns_name:
                return item[0]

        raise ValueError('unknown namespace: "{}"'.format(ns_name))

    def _ns_name_from_id(self, ns_id):
        name = self._ns.get(ns_id, None)

        if not name:
            raise ValueError('unknown namespace id: {}'.format(ns_id))

        return name


class PageParser:

    def __init__(self, element, *, ns_name, custom_prepare=None):
        self._custom_prepare = custom_prepare
        self._ns_id = element.find('{*}ns').text
        self._ns_name = ns_name
        self._text = element.find('{*}revision').find('{*}text').text
        self._title = element.find('{*}title').text
        if int(self._ns_id) > 0:
            self._title = self._title.split(':', 1)[1]

    def prepare(self):
        if self._custom_prepare:
            text = self._custom_prepare(self.text, self.title, self.ns_name)
            if text:
                self._text = text
        pass

    def save(self):
        pass

    @property
    def filename(self):
        name = self.title
        name = name.replace(os.sep, '-')
        keep_chars = (' ', '.', '_', '-')
        return "".join(c for c in name
                       if c.isalnum() or c in keep_chars).rstrip()

    @property
    def ns_id(self):
        return self._ns_id

    @property
    def ns_name(self):
        return self._ns_name

    @property
    def title(self):
        return self._title

    @property
    def text(self):
        return self._text


class FileParser(PageParser):

    def __init__(self, element, *, ns_name, custom_prepare):
        super().__init__(element, ns_name=ns_name, custom_prepare=custom_prepare)
        self._contents = element.find('{*}upload/{*}contents').text
        self._custom_prepare = custom_prepare
        self._encoding = element.find('{*}upload/{*}contents').get('encoding')
        self._filename = element.find('{*}upload/{*}filename').text
        self._ns_id = element.find('{*}ns').text
        self._ns_name = ns_name

    def save(self, basedir):
        if not os.path.isdir(basedir):
            raise ValueError("{}: not a directory".format(basedir))
        if self.encoding != 'base64':
            raise ValueError("{}: expected base64 encoding, got {}"
                             .format(self.title, self.encoding))

        savedir = os.path.join(basedir, self.ns_name)
        try:
            os.mkdir(savedir, mode=0o755)
        except FileExistsError:
            pass
        except OSError:
            raise

        try:
            with open(os.path.join(savedir, self.filename),
                      mode='bw') as f:
                f.write(base64.b64decode(self.contents))
        except OSError:
            raise

    @property
    def contents(self):
        return self._contents

    @property
    def encoding(self):
        return self._encoding

    @property
    def filename(self):
        return self._filename


class WikitextParser(PageParser):

    def prepare(self):
        # Handle page with no text.
        if not self.text:
            return

        # Delete [[Category:FOO]] tags.
        self._text = re.sub(r'\[\[Category:.+\]\]', '', self._text)

        # Delete <nowiki> tags.
        self._text = re.sub(r'</?nowiki>', '', self._text)

        # Delete Table of Contents '__TOC__' marker.
        self._text = re.sub(r'=+ Table of Contents =+\n__TOC__', '', self._text)

        # Put fences around code blocks.
        newtext = []
        in_code = False
        code_pattern = re.compile(r'^\s+\S+', re.ASCII)
        end_pattern = re.compile('^\S', re.ASCII)
        for line in self.text.splitlines(keepends=True):
            if code_pattern.match(line) and not in_code:
                newtext.append('<pre>\n')
                in_code = True
            elif end_pattern.match(line) and in_code:
                newtext[-1] = newtext[-1].rstrip()
                newtext.append('</pre>\n\n')
                in_code = False

            if in_code:
                # Transform bold and italic wiki markup so it renders properly
                # as Markdown. It appears pandoc won't pick up HTML tags inside
                # <pre></pre> blocks so this converts directly to Markdown
                # syntax.
                line = re.sub(r"'''(.+)'''", r'`**\1**`', line)
                line = re.sub(r"''(.+)''", r'`*\1*`', line)

            newtext.append(line)

        # The last few lines of text in the page could be this code block.
        # Force an end to the block to make pandoc happy.
        if in_code:
            newtext.append('</pre>')
        self._text = ''.join(newtext)

        # Remove transclusions.
        self._text = re.sub(r'{{\:.+}}', '', self._text)

        if self._custom_prepare:
            text = self._custom_prepare(self.text, self.title, self.ns_name)
            if text:
                self._text = text

        # Sanity check there are no more wiki templates.
        m = re.search(r'{{.+?}}', self._text)
        if m:
            print("\tWARNING: unhandled wiki template: {}".format(m[0]))

    def save(self, basedir):
        # Handle page with no text.
        if not self.text:
            return

        # Redirect pages have no purpose when imported into Notion. Skip them.
        if re.match('#REDIRECT', self.text):
            return

        if not os.path.isdir(basedir):
            raise ValueError("{}: not a directory".format(savedir))

        savedir = os.path.join(basedir, self.ns_name)
        try:
            os.mkdir(savedir, mode=0o755)
        except FileExistsError:
            pass
        except OSError:
            raise

        try:
            doc = pandoc.read(self.text, format='mediawiki')
        except Exception as e:
            print("\tERROR: pandoc could not read {}: {}"
                  .format(self.title, e), file=sys.stderr)
            return

        try:
            with open(os.path.join(savedir, self.filename + '.md'),
                      mode='bw') as f:
                pandoc.write(doc, file=f, format='gfm',
                             options=['--wrap=none'])
        except OSError as e:
            print("\tERROR: unable to output {}: {}"
                  .format(self.filename, e), file=sys.stderr)


def prepare(text, title, namespace):
        if namespace != 'Main':
            return None

        # Remove {{anchor}} template.
        text = re.sub(r'{{anchor\|.+}}', '', text)

        # Transform {{Attention}} template.
        text = re.sub(r'{{Attention}}',
                      '💡 ',
                      text,
                      flags=re.IGNORECASE)

        # Transoform {{Book}} template.
        text = re.sub(r'{{Book\|(.+)\|(\d+)}}',
                      r"(source: ''/1''/ISBN /2)",
                      text,
                      flags=re.IGNORECASE)

        # Transform {{Ciscobug}} template.
        text = re.sub(r'{{Ciscobug\|(.+)}}',
                      r'[https://bst.cloudapps.cisco.com/bugsearch/bug/\1]',
                      text)

        # Transform {{CiscoCase}} template.
        text = re.sub(r'{{CiscoCase\|(\d+)}}',
                      r'[http://tools.cisco.com/ServiceRequestTool/query/QueryCaseSearchAction.do?method=doQueryByCase&caseType=ciscoServiceRequest&SRNumber=\1 \1]',
                      text)

        # Transform {{CiscoTACCC} template.
        text = re.sub(r'{{CiscoTACCC\|(\w+)}}',
                      r'[http://www.ciscotaccc.com/lanswitching/showcase?case=\1]',
                      text)

        # Transform {{href}} template.
        text = re.sub(r'{{href\|(\S+)\s+([^\|]+)\|(.+)}}',
                      r'[\1 \2] (\3)',
                      text)

        # Transform {{JuniperKB}} template.
        text = re.sub(r'{{JuniperKB\|(\d+)\|(.+)}}',
                      r'[http://kb.juniper.net/index?page=content&id=KB\1 \2]',
                      text)

        # Transform {{leftoffat}} template.
        text = re.sub(r'{{leftoffat\|(.+)}}',
                      r'<aside>💡 You left off at: \1</aside>',
                      text)

        # Transform {{Msgid}} template.
        text = re.sub(r'{{Msgid\|(\S+)\|(.+)}}',
                      r'[\1 \2]',
                      text,
                      flags=re.IGNORECASE)

        # Transform {{MSKB}} template.
        text = re.sub(r'{{MSKB\|(\d+)\|(.+)}}',
                      r'[http://support.microsoft.com/kb/\1 \2]',
                      text)

        # Transform {{Needsclarification}} template.
        text = re.sub('{{Needsclarification}}',
                      '⚠️  ',
                      text,
                      flags=re.IGNORECASE)

        # Transform {{Needswork}} template.
        text = re.sub('{{Needswork}}',
                      '🚧 ',
                      text,
                      flags=re.IGNORECASE)

        # Transform {{RFC}} template.
        text = re.sub(r'{{RFC\|([-\w\d]+)(?:\|(.+))?}}',
                      r'[https://tools.ietf.org/html/\1 RFC \1 \2]',
                      text)

        # Transform {{source}} template.
        text = re.sub(r'{{source\|(.+?)}}',
                      r'(source: \1)',
                      text,
                      flags=re.IGNORECASE)

        # Transform {{sourcelink}} template. In some places, I've incorrectly
        # used the template which accounts for the two search patterns.
        text = re.sub(r'{{sourcelink\|(\S+)\|(.+?)}}',
                      r'(source: [\1 \2])',
                      text)
        # This pattern must come second. There is a corner case where if both
        # patterns appear on the same line, this pattern will gobble up both
        # plus the intervening text and make a right mess of the result.
        text = re.sub(r'{{sourcelink\|(\S+)\s(.+?)\|.+?}}',
                      r'(source: [\1 \2])',
                      text)

        # Transform {{VMwareKB}} template.
        text = re.sub(r'{{VMwareKB\|(\d+)(?:\|(.+))?}}',
                      r'[http://kb.vmware.com/kb/\1 \2]',
                      text)

        return text


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prepare MediaWiki XML dump")
    parser.add_argument('-outdir', type=str,
                        help='the directory to save wikitext files in')
    parser.add_argument('xmlfile', type=str,
                        help='the path to the XML dump file')
    args = parser.parse_args()

    xml_parser = XmlParser(args.xmlfile, args.outdir,
                           custom_prepare=prepare)
    xml_parser.parse()
