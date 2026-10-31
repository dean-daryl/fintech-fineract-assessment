"""Prints every test case and a per-suite count from Gradle's JUnit XML results."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(paths: list[str]) -> int:
    failed = 0
    total = 0
    for path in paths:
        suite = ET.parse(path).getroot()
        tests, failures, errors, skipped = (int(suite.get(key, 0)) for key in ("tests", "failures", "errors", "skipped"))
        print(f"{suite.get('name')}: {tests} tests, {failures} failures, {errors} errors, {skipped} skipped")
        for case in suite.iter("testcase"):
            outcome = "FAILED" if case.find("failure") is not None or case.find("error") is not None else "SKIPPED" if case.find("skipped") is not None else "PASSED"
            print(f"  {outcome} {case.get('name')} ({case.get('time')}s)")
        failed += failures + errors
        total += tests
    print(f"\n{total} tests, {failed} failed")
    return 1 if failed or not total else 0


if __name__ == "__main__":
    sys.exit(main([str(Path(p)) for p in sys.argv[1:]]))
