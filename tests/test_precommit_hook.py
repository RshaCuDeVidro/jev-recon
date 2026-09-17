"""The pre-commit hook that refuses to commit target data.

It exists because `git add -A` shipped a 24,771 host list of a government target
and a 249 host list from another engagement into a dozen commits. A file name is
not a signal,
so the hook reads content, which means its two failure modes matter: refusing a
real commit (annoying) and letting a hostname list through (the leak).
"""

import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "scripts", "hooks", "pre-commit")


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True)


class TestPreCommitHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        run("git init -q", self.repo)
        run("git config user.email t@t && git config user.name T", self.repo)
        self.hook = run(f"cp {HOOK} .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit",
                        self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self):
        return run("git commit -m x", self.repo)

    def stage(self, name, content):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        run(f"git add -f {name}", self.repo)

    def test_refuses_a_hostname_list(self):
        hosts = "".join(f"host{i}.target.com\n" for i in range(25))
        self.stage("hosts.txt", hosts)
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("looks like target data", result.stderr)
        self.assertIn("git rm --cached", result.stderr)

    def test_refuses_hostnames_inside_json(self):
        body = "[" + ",".join('{"hostname":"h%d.target.gov.br"}' % i for i in range(25)) + "]"
        self.stage("run-output.json", body)
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)

    def test_allows_docs_code_and_few_hostnames(self):
        for name, content in (
            ("README.md", "the host api.example.com scored 0.62\n" * 40),
            ("pipeline.py", 'HOSTS = ["a.example.com",\n' + '"b.example.com",\n' * 40 + "]\n"),
            ("tiny.json", '{"a.example.com": {"http_status": 200}}'),
        ):
            self.stage(name, content)
            result = self.commit()
            self.assertEqual(result.returncode, 0,
                             f"{name} was wrongly refused: {result.stderr}")

    def test_examples_and_tests_are_exempt(self):
        hosts = "".join(f"host{i}.fake.example\n" for i in range(30))
        self.stage("examples/list.txt", hosts)
        self.stage("tests/fixtures/other.txt", hosts)
        self.assertEqual(self.commit().returncode, 0)

    def test_the_installed_hook_is_the_tracked_one(self):
        installed = os.path.join(ROOT, ".git", "hooks", "pre-commit")
        self.assertTrue(os.access(installed, os.X_OK), "hook not installed as executable")
        with open(installed) as a, open(HOOK) as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
