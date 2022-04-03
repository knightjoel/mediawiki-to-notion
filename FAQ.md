# Frequently Asked Questions

## Does this solution connect to my wiki?

The solution does not connect to the wiki. Wiki content is exported from the wiki using a MediaWiki PHP script to export wiki content into an XML file. The XML file is processed by this solution either on the MediaWiki server or on any server which can meet the dependencies for this solution.

## What will my AWS costs be to run this solution?

The AWS services used by this solution have a pay-for-use cost model. Your cost will be driven by your usage. Additional factors such as whether your AWS account is still within the free tier and your existing usage of AWS services will determine your final cost.

When you no longer use this solution, follow the instructions in [README.md](README.md) to remove it from your AWS account in order to stop all charges.


## How do I see what’s going on with the import pipeline?

Use the included CloudWatch dashboard for monitoring the performance of the pipeline. Log into your AWS account, navigate to CloudWatch, click Dashboards, and select `MediaWikiToNotion`.

You can also watch from your Notion app to see new pages being created and blocks being added to the pages.

## The dashboard shows pages in `FAIL` state. How do I see what went wrong?

Pages enter `FAIL` state when something went wrong with their upload to Notion. In the AWS console, open CloudWatch, browse to `Log groups`, and then open `/aws/lambda/UploadNotionBlocks`. Click `Search all` and search for the name of the page (note: search is case sensitive). Review the log messages to determine root cause of the error.

## I see `Unable to download file 'somefile.jpg'` in the UploadNotionBlocks logs. What does this mean?

The solution downloads media files such as JPGs, PDFs, or others from the `File` directory in the Amazon S3 bucket. These files come from the wiki pages as embedded media. These media files need to be retrieved from the bucket before they can be imported into Notion with their associated page. The error means that the solution could not download the file from the bucket.

Verify the capitalization of the filename in the Markdown file matches the capitalization of the file in the bucket. Amazon S3 is case sensitive so the case in the Markdown must match the filename in the bucket. Once you’ve corrected the filename in the Markdown, upload the Markdown file to the bucket again to trigger a new upload attempt.


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

Wikipage metadata such as the timestamp when it was created could be preserved. The non-public Notion API appears to allow setting an arbitrary `created_time` property.

There is almost certainly opportunity for better visibility into error conditions which the pipeline encounters.

