"""
The three build numbers must move together (2026-10-26: APP_BUILD and the
?v= asset tags went to 35 but app.js's CLIENT_BUILD stayed at 33, so every
page load showed "This page is out of date" and Ctrl+F5 could not fix it).

    app/main.py        APP_BUILD = N
    static/app.js      const CLIENT_BUILD = N;
    static/index.html  every /static/...?v=N

Run:  python tests/test_build_numbers.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


server = int(re.search(r"^APP_BUILD = (\d+)", read("app", "main.py"), re.M).group(1))
client = int(re.search(r"^const CLIENT_BUILD = (\d+);", read("static", "app.js"), re.M).group(1))
assets = {int(v) for v in re.findall(r'/static/[\w.-]+\?v=(\d+)', read("static", "index.html"))}

problems = []
if client != server:
    problems.append(f"static/app.js CLIENT_BUILD={client} but app/main.py APP_BUILD={server}")
if assets != {server}:
    problems.append(f"static/index.html ?v= values {sorted(assets)} but APP_BUILD={server}")

for p in problems:
    print("  FAIL", p)
if not problems:
    print(f"  ok   all three build numbers are {server}")
sys.exit(1 if problems else 0)
