#!/usr/bin/env python3
"""Build typo_words.json for the chat's typo repair (hive_chat.fix_typos).

  python3 build_typo_words.py /usr/share/dict/american-english

targets: the words the chat's own patterns route on ("battery", "temperature",
         "germination"...) - every word of 5+ letters in a regular expression in
         hive_chat.py that is a real English word.
protect: real English words that are close enough to a target to be "repaired"
         into it ("thirsty" is 92 % like "thirty", "whether" like "weather") -
         they are never changed. Only this short list ships, not the dictionary.
known:   pattern words the dictionary lacks ("reboot") - never changed either.

Any word list with one word per line works (Debian/Ubuntu's wamerican is the one
used here). Rerun it after adding patterns to hive_chat.py.
"""
import ast
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CUTOFF = 0.8            # the same cutoff hive_chat.fix_typos uses
RE_CALLS = {"compile", "search", "match", "fullmatch", "sub", "findall", "finditer", "split"}


def pattern_words(src):
    """Lower-case words of 5+ letters in the string literals given to re.*() or kept in a *_Q / *_WORDS constant."""
    out = set()
    for node in ast.walk(ast.parse(src)):
        texts = []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in RE_CALLS \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "re" and node.args:
            texts = [a.value for a in ast.walk(node.args[0]) if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        for t in texts:
            # drop escapes (\b, \w, \s, \d) before taking words: "\bwater\b" is "water", not "bwater"
            t = re.sub(r"\\[A-Za-z]", " ", t)
            out.update(w.lower() for w in re.findall(r"[A-Za-z]{5,}", t))
    return out


def main(dict_path):
    words = {w.strip().lower() for w in open(dict_path, encoding="utf-8", errors="ignore")
             if re.fullmatch(r"[A-Za-z]+", w.strip())}
    src = open(os.path.join(HERE, "hive_chat.py"), encoding="utf-8").read()
    found = pattern_words(src)
    # a stem in a pattern ("calibrat\w*", "germinat\w*") stands for the real words it starts
    stems = [w for w in found if w not in words and len(w) >= 5]
    targets = sorted({w for w in found if w in words} |
                     {w for w in words for st in stems if w.startswith(st) and len(w) <= len(st) + 4})
    by_len = {}
    for w in words:
        by_len.setdefault((w[0], len(w)), []).append(w)
    protect = set()
    for t in targets:
        # fix_typos only repairs into a word with the same first letter, so only those need protecting
        pool = [w for k in range(len(t) - 3, len(t) + 4) for w in by_len.get((t[0], k), [])]
        protect.update(difflib.get_close_matches(t, pool, n=50, cutoff=CUTOFF))
    protect -= set(targets)
    out = {"_about": "built by build_typo_words.py from %s; targets = words the chat's patterns route on, protect = real "
                     "words close to one (never changed)" % os.path.basename(dict_path),
           "targets": targets, "protect": sorted(protect),
           # pattern words no dictionary has ("reboot", "dbm"...): never changed, and their inflections neither
           "known": sorted(w for w in found if w not in words)}
    with open(os.path.join(HERE, "typo_words.json"), "w") as f:
        json.dump(out, f, indent=0)
        f.write("\n")
    print("%d targets, %d protected words" % (len(targets), len(protect)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
