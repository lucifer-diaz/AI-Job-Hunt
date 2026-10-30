# Bundled data

`english_words.txt.gz` — lower-case alphabetic English words (one per line), the union of the GCIDE and Webster's
Second word sets from the `english-words` package (2.0.2, MIT licence, https://github.com/mwiens91/english-words-py,
https://pypi.org/project/english-words/). Read by `app/services/text_repair.py` to repair broken ligature glyphs in
extracted PDF text. Rebuild by reading the package's `gcide_alpha_lower` and `web2_alpha_lower` sets.
