#!/usr/bin/env python3
"""Wrap index.html (the artifact source) into a standalone page for Netlify."""
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAVICON = ("data:image/svg+xml,"
  "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
  "%3Crect width='64' height='64' rx='14' fill='%231F4372'/%3E"
  "%3Cpath d='M16 33l10 10 22-24' fill='none' stroke='%233ECB92' stroke-width='8'"
  " stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Tap the rules for your setup; the button unlocks when they're all met.">
<meta name="theme-color" content="#EDF0F4" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E1319" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Pre-Flight">
<meta name="mobile-web-app-capable" content="yes">
<link rel="icon" href="%s">
<link rel="apple-touch-icon" href="%s">
""" % (FAVICON, FAVICON)

# the artifact host normally supplies these; standalone has to carry them itself
EXTRA = """<style>
  html{color-scheme:light dark; -webkit-text-size-adjust:100%}
  body{padding-top:env(safe-area-inset-top,0px)}
  [hidden]{display:none!important}
</style>
</head>
<body>
"""

src = (HERE / "index.html").read_text()
i = src.index('<div class="wrap">')
out = HEAD + src[:i] + EXTRA + src[i:].rstrip() + "\n</body>\n</html>\n"
(HERE / "preflight-netlify.html").write_text(out)
print("wrote preflight-netlify.html —", len(out), "bytes")
