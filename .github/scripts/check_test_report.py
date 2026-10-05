"""Fail unless the pytest JUnit report shows enough tests and nothing skipped, xfailed or errored.

Usage: python check_test_report.py REPORT.xml MIN_TESTS
"""

import sys
import xml.etree.ElementTree as ET

report, min_tests = sys.argv[1], int(sys.argv[2])
suite = ET.parse(report).getroot().find("testsuite")
counts = {key: int(suite.get(key)) for key in ("tests", "skipped", "errors", "failures")}
print(counts)

# pytest records xfailed tests as skipped in the JUnit report, so "skipped" covers both.
problems = []
if counts["tests"] < min_tests:
    problems.append(f"only {counts['tests']} tests collected, expected at least {min_tests}")
for key in ("skipped", "errors", "failures"):
    if counts[key]:
        problems.append(f"{counts[key]} {key}")

if problems:
    sys.exit("Test check failed: " + "; ".join(problems))
print("Test check passed.")
