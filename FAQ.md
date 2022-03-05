# Frequently Asked Questions

## How do I import pages from additional MediaWiki namespaces?

By default, the `process-mw-dump.py` script will only export content from 3 [namespaces](https://www.mediawiki.org/wiki/Help:Namespaces): `Category`, `File`, and `Main`.

To export pages from additional namespaces, modify `process-mw-dump.py`'s `XmlParser.parse()` method to match on additional namespaces. For example, to process pages in a namespace named `Documentation`, add the following code snippet at around line 68:

```python
68    elif ns_id == self._ns_id_from_name('Documentation'):
69        parser = WikitextParser(
70	          element,
71	          ns_name=ns_name,
72            custom_prepare=self._custom_prepare
73        )
```

You can optionally subclass either the `PageParser` or `WikitextParser` classes to customize how the pages in the namespace are processed most notably by providing a custom `prepare()` method.

## How do I add support for processing my custom wiki templates?

MediaWiki’s wikitext supports inline templates which allow for inserting bits of text or markup by  writing a short template tag. For example, a template which looks like `{{RFC|1925|The 12 Networking Truths}}` could be used to insert a hyperlink to the RFC document such as `<a href="https://datatracker.ietf.org/doc/html/rfc1925">The 12 Networking Truths</a>`. Pandoc, which does the wikitext→Markdown conversion for this solution does not expand wiki templates. The resulting Markdown document will contain the literal template text (`{RFC|1925|The 12 Networking Truths}}`) instead of the desired hyperlink.

The `process-mw-dump.py` script has a mechanism in it to handle your wiki templates. You can add, delete, or modify the template transformation rules in `process-mw-dump.py`'s `custom_prepare()` function to suit the templates in use on your wiki. The function contains a number of examples taken from a real wiki.

## What’s the maximum size of wiki page I can import?

There is no hard limit. Each page is broken down into Notion blocks prior to importing and the blocks are imported one by one. The solution is designed to ensure that even very large pages can be imported.

## What’s the maximum supported size for embedded files?

The maximum total size for file attachments on a single wiki page is approximately 500MB.

## How do I speed up the import?

Pages are processed in serial order to avoid hammering the Notion API with dozens or hundreds of concurrent imports. The import pipeline is also designed to operate without supervision. Upload your files to the bucket and let the import process run. Come back after a nice cup of ☕ or a 🏃.

## How could the solution architecture be improved?

Increase scale for handling very large wikis by having the Step Function state machine restart itself prior to reaching 25,000 events (which is a [hard limit](https://docs.aws.amazon.com/step-functions/latest/dg/limits-overview.html#service-limits-state-machine-executions) for standard state machines). This would only be relevant for importing > ~500,000 blocks in a single page.

There is almost certainly opportunity for better visibility into error conditions which the pipeline encounters.

## How do I see what’s going on with the import pipeline?

Use the included CloudWatch dashboard for monitoring the performance of the pipeline. Log into your AWS account, navigate to CloudWatch, click Dashboards, and select `MediaWikiToNotion`.

You can also watch from your Notion app to see new pages being created and blocks being added to the pages.
