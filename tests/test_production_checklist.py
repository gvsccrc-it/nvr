"""The production checklist (#553) stays in step with the Compose files, and holds nothing of a real site.

Its hardening table names Compose keys: one marked "in place" must be set in the file it names, and one marked "add this" must not be,
so that when someone adds it the page has to be updated too."""
from __future__ import annotations

import ipaddress
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "production-checklist.md"
ROW = re.compile(r"^\| `(?P<key>[a-z_]+)` \| (?P<service>[a-z]+) \((?P<file>compose[a-z.]*\.yaml)\) \| (?P<status>in place|add this) \|", re.MULTILINE)
DOCUMENTATION_RANGES = [ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8")]


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.page = PAGE.read_text(encoding="utf-8")

    def test_the_readme_links_the_checklist_before_start_the_stack(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/production-checklist.md", readme)
        self.assertLess(readme.index("docs/production-checklist.md"), readme.index("## Start the stack"))

    def test_it_covers_every_item_the_ticket_lists(self):
        for heading in ("Network", "First run", "The image", "Backups", "Clocks", "Disks", "Memory", "Hardening", "Upgrade and rollback", "Sizing", "Monitoring"):
            self.assertRegex(self.page, rf"(?m)^## \d+\. {re.escape(heading)}$")

    def test_each_hardening_key_is_in_its_compose_file_or_marked_add_this(self):
        rows = list(ROW.finditer(self.page))
        self.assertGreaterEqual(len(rows), 10)
        for row in rows:
            with self.subTest(key=row["key"], service=row["service"], file=row["file"]):
                services = yaml.safe_load((ROOT / row["file"]).read_text(encoding="utf-8"))["services"]
                self.assertIn(row["service"], services)
                present = row["key"] in services[row["service"]]
                if row["status"] == "in place":
                    self.assertTrue(present, f"the page says {row['key']} is set for {row['service']} in {row['file']}, and it is not")
                else:
                    self.assertFalse(present, f"{row['key']} is now set for {row['service']} in {row['file']}: mark it \"in place\" on the page")

    def test_no_address_or_domain_of_a_real_site(self):
        for text in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d+)?\b", self.page):
            address = ipaddress.ip_network(text, strict=False)
            self.assertTrue(any(address.subnet_of(net) for net in DOCUMENTATION_RANGES), f"{text} is not a documentation address")
        for host in re.findall(r"https?://([a-z0-9.-]+)", self.page):
            self.assertTrue(host.endswith("example.test"), host)


if __name__ == "__main__":
    unittest.main()
