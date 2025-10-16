Sati utilities
================

New command: decode-and-merge
-----------------------------

This command extracts a ZIP file containing PDFs (some may be password-protected), attempts to decrypt each PDF with the supplied password, and merges all readable pages into a single output PDF.

Example:

	sati decode-and-merge --zip /path/to/files.zip --password mypass --output /path/to/merged.pdf

Notes:
• If a PDF cannot be decrypted with the provided password it will be skipped.
• Files are merged in filename order.
# Sati
