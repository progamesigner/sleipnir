from pathlib import Path
import json
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))

import update  # noqa: E402


def temurin_release(tag, arches=("x64", "aarch64"), prerelease=False):
    release = tag.removeprefix("jdk-").replace("+", "_")
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "html_url": f"https://github.com/adoptium/temurin21-binaries/releases/tag/{tag}",
        "assets": [{"name": f"OpenJDK21U-jdk_{arch}_linux_hotspot_{release}.tar.gz"} for arch in arches]
                  + [{"name": f"OpenJDK21U-jre_x64_linux_hotspot_{release}.tar.gz"}],
    }


class UpdateTests(unittest.TestCase):
    def candidates(self, spec, current, responses, ubuntu="26.04"):
        urls = []

        def fetch(_, url):
            urls.append(url)
            return responses[url]

        with mock.patch.object(update, "fetch_text", fetch):
            records, _ = update.candidates("", spec["kind"], spec["source"], spec["prefix"], current, ubuntu)
        return [record["version"] for record in records], urls

    def spec(self, name):
        return next(spec for spec in update.CATALOG if spec["name"] == name)

    def test_temurin_build_orders_after_release(self):
        self.assertLess(update.version("21.0.12+8"), update.version("21.0.12.1+1"))
        self.assertLess(update.version("21.0.11+10"), update.version("21.0.12+8"))
        self.assertEqual(update.version("36.1.0"), (36, 1, 0, 0, 0))
        self.assertIsNone(update.version("21"))
        self.assertIsNone(update.version("36.0.0-rc1"))

    def test_replace_version_accepts_temurin_release(self):
        content = "JAVA_VERSION=21.0.11+10\n"
        self.assertEqual(update.replace_version(content, "JAVA_VERSION", "21.0.11+10", "21.0.12.1+1"),
                         "JAVA_VERSION=21.0.12.1+1\n")

    def test_temurin_uses_feature_repository_and_requires_both_architectures(self):
        url = "https://api.github.com/repos/adoptium/temurin21-binaries/releases?per_page=100&page=1"
        rows = [
            temurin_release("jdk-21.0.13+3-ea-beta", prerelease=True),
            temurin_release("jdk-21.0.12.1+1", arches=("x64",)),
            temurin_release("jdk-21.0.12+8"),
            temurin_release("jdk-21.0.11+10"),
        ]
        versions, urls = self.candidates(self.spec("JAVA_VERSION"), "21.0.11+10", {url: json.dumps(rows)})
        self.assertEqual(urls, [url])
        self.assertEqual(versions, ["21.0.12+8", "21.0.11+10"])

    def test_swift_requires_image_ubuntu_on_both_architectures(self):
        spec = self.spec("SWIFT_VERSION")
        rows = [
            {"name": "6.3.3", "tag": "swift-6.3.3-RELEASE",
             "platforms": [{"name": "Ubuntu 24.04", "archs": ["x86_64", "aarch64"]}]},
            {"name": "6.4.0", "tag": "swift-6.4.0-RELEASE",
             "platforms": [{"name": "Ubuntu 26.04", "archs": ["x86_64", "aarch64"]}]},
            {"name": "6.4.1", "tag": "swift-6.4.1-RELEASE",
             "platforms": [{"name": "Ubuntu 26.04", "archs": ["x86_64"]}]},
        ]
        versions, _ = self.candidates(spec, "6.3.3", {spec["source"]: json.dumps(rows)})
        self.assertEqual(versions, ["6.4.0"])

    def test_android_build_tools_skip_previews(self):
        spec = self.spec("ANDROID_BUILD_TOOLS_VERSION")
        xml = "".join(f'<remotePackage path="{path}">' for path in (
            "build-tools;36.1.0", "build-tools;37.0.0-rc1", "platforms;android-36", "build-tools;36.0.0"))
        versions, _ = self.candidates(spec, "36.0.0", {spec["source"]: xml})
        self.assertEqual(versions, ["36.1.0", "36.0.0"])

    def test_versions_file_is_fully_mapped(self):
        content = (ROOT / "versions").read_text()
        known = {spec["name"] for spec in update.CATALOG} | update.UNPINNED.keys() | {"DEVCONTAINERS_REF", "VSCODE_COMMIT"}
        names = {line.split("=", 1)[0] for line in content.splitlines() if line}
        self.assertEqual(names - known, set())


if __name__ == "__main__":
    unittest.main()
