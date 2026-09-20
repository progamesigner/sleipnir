from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUN = subprocess.run


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.api_socket = str(self.root / "tailscaled.sock")
        self.supervisor = self.root / "s6-svstat"
        self.supervisor.write_text('#!/bin/sh\necho "${TEST_SERVICE_UP:-true}"\n')
        self.supervisor.chmod(0o755)
        source = (ROOT / "rootfs/usr/local/bin/sleipnir-healthcheck").read_text()
        self.script = self.root / "healthcheck"
        self.script.write_text(source.replace("/command/s6-svstat", str(self.supervisor))
                               .replace("/run/tailscale/tailscaled.sock", self.api_socket))
        self.env = {
            "PATH": "/usr/bin:/bin",
            "SLEIPNIR_ENABLE_HERDR": "0",
            "SLEIPNIR_ENABLE_MOSHI": "0",
            "SLEIPNIR_ENABLE_TAILSCALE": "1",
        }

    def run_check(self, mode="live"):
        return RUN(["/bin/bash", str(self.script), mode], env=self.env,
                   capture_output=True, text=True, timeout=10)

    def api_check(self, body, mode="live", code=200):
        done = threading.Event()
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(self.api_socket)
            server.listen()
            server.settimeout(5)
            def serve():
                with server.accept()[0] as conn:
                    conn.recv(4096)
                    if body is None:
                        done.wait(5)
                    else:
                        data = body.encode()
                        conn.sendall(f"HTTP/1.1 {code} Test\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                return self.run_check(mode)
            finally:
                done.set()
                thread.join(6)
                Path(self.api_socket).unlink()

    def test_healthy(self):
        for mode in ("live", "ready"):
            result = self.api_check('{"BackendState":"Running","TailscaleIPs":["100.64.0.1"]}', mode)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_auth_or_stopped_does_not_restart(self):
        for state in ("NeedsLogin", "NeedsMachineAuth", "Stopped", "Starting"):
            for mode, expected in (("live", 0), ("ready", 1)):
                with self.subTest(state=state, mode=mode):
                    result = self.api_check('{"BackendState":"' + state + '"}', mode)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_running_without_address_is_not_ready(self):
        self.assertEqual(self.api_check('{"BackendState":"Running"}', "ready").returncode, 1)

    def test_malformed_status_fails(self):
        for body in ('{}', '[]', '{"BackendState":null}', 'not-json'):
            with self.subTest(body=body):
                self.assertEqual(self.api_check(body).returncode, 1)

    def test_dead_or_unresponsive_supervisor_fails(self):
        self.env["TEST_SERVICE_UP"] = "false"
        self.assertEqual(self.run_check().returncode, 1)
        self.supervisor.write_text('#!/bin/sh\nsleep 30\n')
        start = time.monotonic()
        self.assertEqual(self.run_check().returncode, 1)
        self.assertLess(time.monotonic() - start, 3)

    def test_disabled_services_are_skipped(self):
        self.env["SLEIPNIR_ENABLE_TAILSCALE"] = "0"
        self.supervisor.unlink()
        self.assertEqual(self.run_check("ready").returncode, 0)

    def test_moshi_loopback_and_closed_port(self):
        self.env.update(SLEIPNIR_ENABLE_TAILSCALE="0", SLEIPNIR_ENABLE_MOSHI="1")
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.env["MOSHI_LISTEN"] = "0.0.0.0:" + str(server.getsockname()[1])
            result = self.run_check()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_check().returncode, 1)

    def test_moshi_ipv6_wildcard(self):
        self.env.update(SLEIPNIR_ENABLE_TAILSCALE="0", SLEIPNIR_ENABLE_MOSHI="1")
        with socket.socket(socket.AF_INET6) as server:
            server.bind(("::1", 0))
            server.listen()
            self.env["MOSHI_LISTEN"] = "[::]:" + str(server.getsockname()[1])
            result = self.run_check()
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_error_and_timeout(self):
        self.assertEqual(self.api_check('error', code=500).returncode, 1)
        start = time.monotonic()
        self.assertEqual(self.api_check(None).returncode, 1)
        self.assertLess(time.monotonic() - start, 5)

    def test_usage(self):
        result = self.run_check("invalid")
        self.assertEqual(result.returncode, 2)
        result = RUN(["/bin/bash", str(self.script)], capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 2)


class FinishTests(unittest.TestCase):
    def test_shutdown_only_on_unexpected_enabled_service_exit(self):
        for service in ("tailscaled", "herdr"):
            for enabled, wanted in (("0", "true"), ("1", "false"), ("1", "true")):
                with self.subTest(service=service, enabled=enabled, wanted=wanted), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    script = (ROOT / f"rootfs/etc/s6-overlay/s6-rc.d/{service}/finish").read_text()
                    script = script.replace("/run/s6-linux-init-container-results/exitcode", str(root / "exitcode"))
                    script = script.replace("/run/s6/basedir/bin/halt", str(root / "halt"))
                    (root / "finish").write_text(script)
                    (root / "s6-svstat").write_text(f"#!/bin/sh\necho {wanted}\n")
                    (root / "halt").write_text(f"#!/bin/sh\ntouch {root}/halted\n")
                    (root / "s6-svstat").chmod(0o755)
                    (root / "halt").chmod(0o755)
                    flag = "TAILSCALE" if service == "tailscaled" else "HERDR"
                    RUN(["sh", str(root / "finish"), "2", "0"], check=True, capture_output=True,
                        env={"PATH": directory + ":/usr/bin:/bin", "SLEIPNIR_ENABLE_" + flag: enabled})
                    should_halt = enabled == "1" and wanted == "true"
                    self.assertEqual((root / "halted").exists(), should_halt)
                    self.assertEqual((root / "exitcode").exists(), should_halt)
                    if should_halt:
                        self.assertEqual((root / "exitcode").read_text(), "1\n")


if __name__ == "__main__":
    unittest.main()
