#!/usr/bin/env python3
"""
Automates adding a new reverse-proxy site on VM1 (Apache).
Uses EXACT templates from iskcontirupati.conf and iskcontirupati-le-ssl.conf.

Usage:
    sudo python3 add_proxy_site.py [domain]
Run tests: python3 -m unittest add_proxy_site -v

Note: Certbot is skipped (commented out) — run manually after Apache reload:
  - For new cert: sudo certbot --apache -d {domain} -d www.{domain}
  - For existing cert: sudo certbot install --apache --cert-name {domain} -d {domain} -d www.{domain}
"""

import argparse
import os
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

# ─── CONFIG ───────────────────────────────────────────────────────────────────
EXPECTED_IP = "13.204.103.142"
SITES_AVAILABLE = "/etc/apache2/sites-available"
SITES_ENABLED = "/etc/apache2/sites-enabled"
BACKENDS = {
    "1": "www.temples.bio",
    "2": "www.defend.ink",
}
# ──────────────────────────────────────────────────────────────────────────────


def run(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    print(f"\n▶  {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        print(f"✖  Command failed (exit {result.returncode}).")
        sys.exit(result.returncode)
    return result


# ─── STEP 1: DNS CHECK ────────────────────────────────────────────────────────


def check_dns(domain: str) -> None:
    print(f"\n[1/4] DNS check: '{domain}' must resolve to {EXPECTED_IP}")
    try:
        results = socket.getaddrinfo(domain, None)
        resolved_ips = {r[4][0] for r in results}
        print(f"      Resolved IPs: {', '.join(resolved_ips)}")
    except socket.gaierror as exc:
        print(f"✖  DNS lookup failed: {exc}")
        sys.exit(1)

    if EXPECTED_IP not in resolved_ips:
        print(
            f"✖  '{domain}' does not point to {EXPECTED_IP}.\n"
            f"   Update the DNS A record first, then re-run."
        )
        sys.exit(1)

    print(f"✔  DNS OK")


# ─── STEP 2: SAFETY — REFUSE TO OVERWRITE EXISTING CONFIGS ───────────────────


def check_no_existing_config(domain: str) -> None:
    """
    Exit immediately if config files for this domain already exist.
    This is the primary guard against touching existing sites.
    """
    conflicts = []
    for filename in [f"{domain}.conf", f"{domain}-le-ssl.conf"]:
        path = os.path.join(SITES_AVAILABLE, filename)
        if os.path.exists(path):
            conflicts.append(path)

    if conflicts:
        print("\n✖  Config file(s) already exist for this domain:")
        for p in conflicts:
            print(f"     {p}")
        print("   Refusing to overwrite. Handle manually if needed.")
        sys.exit(1)

    print("✔  No existing config conflict — safe to proceed")


# ─── STEP 3: WRITE CONFIGS (EXACT TEMPLATES) ─────────────────────────────────


def write_http_conf(domain: str) -> str:
    conf_path = os.path.join(SITES_AVAILABLE, f"{domain}.conf")
    content = f"""# Port 80 - redirect HTTP to HTTPS
<VirtualHost *:80>
    ServerName {domain}
    ServerAlias www.{domain}

    RewriteEngine On
    RewriteRule ^ https://%{{SERVER_NAME}}%{{REQUEST_URI}} [L,R=301]

    ErrorLog ${{APACHE_LOG_DIR}}/{domain}-error.log
    CustomLog ${{APACHE_LOG_DIR}}/{domain}-access.log combined
RewriteCond %{{SERVER_NAME}} ={domain}
RewriteRule ^ https://%{{SERVER_NAME}}%{{REQUEST_URI}} [END,NE,R=permanent]
</VirtualHost>"""
    with open(conf_path, "x") as f:  # "x" = fail if file exists (extra safety)
        f.write(content)
    print(f"✔  Written: {conf_path}")
    return f"{domain}.conf"


def write_ssl_conf(domain: str, backend: str) -> str:
    conf_path = os.path.join(SITES_AVAILABLE, f"{domain}-le-ssl.conf")
    content = f'''<IfModule mod_ssl.c>
<VirtualHost *:443>
    ServerName {domain}

    ErrorLog ${{APACHE_LOG_DIR}}/{domain}-error.log
    CustomLog ${{APACHE_LOG_DIR}}/{domain}-access.log combined

    # Reverse proxy
    SSLProxyEngine On
    SSLProxyVerify none
    SSLProxyCheckPeerCN off
    SSLProxyCheckPeerName off

    ProxyPreserveHost Off
    RequestHeader set Host "{backend}"

    # Forward all requests, preserve the path
    ProxyPass "/" "https://{backend}/out/{domain}/"
    ProxyPassReverse "/" "https://{backend}/out/{domain}/"

    # Avoid compression issues
    RequestHeader unset Accept-Encoding

    <Directory /var/www/html>
        AllowOverride All
        Require all granted
    </Directory>


    # Certbot will append: Include, ServerAlias, and cert lines here


SSLCertificateFile    /etc/ssl/certs/ssl-cert-snakeoil.pem
SSLCertificateKeyFile /etc/ssl/private/ssl-cert-snakeoil.key
</VirtualHost>
</IfModule>'''
    with open(conf_path, "x") as f:  # "x" = fail if file exists (extra safety)
        f.write(content)
    print(f"✔  Written: {conf_path}")
    return f"{domain}-le-ssl.conf"


# ─── STEP 4: ENABLE + CONFIG TEST + RELOAD ───────────────────────────────────


def enable_and_reload(domain: str, http_conf: str, ssl_conf: str) -> None:
    print(f"\n[3/4] Enabling sites")
    run(["a2ensite", http_conf])
    run(["a2ensite", ssl_conf])

    print("\n[4/4] Validating Apache config before reload")
    result = subprocess.run(
        ["apache2ctl", "configtest"], capture_output=True, text=True
    )
    print(result.stdout or result.stderr)

    if result.returncode != 0:
        print(
            "✖  Apache config test FAILED. Rolling back new site to protect existing sites."
        )
        _rollback(domain, http_conf, ssl_conf)
        sys.exit(1)

    print("✔  Config test passed — reloading Apache")
    run(["systemctl", "reload", "apache2"])
    print("✔  Apache reloaded (existing sites untouched)")


# ─── STEP 5: CERTBOT ─────────────────────────────────────────────────────────


def run_certbot(domain: str, http_conf: str, ssl_conf: str) -> None:
    """
    COMMENTED OUT: Certbot skipped — run manually after script completes.
    For new cert: sudo certbot --apache -d {domain} -d www.{domain}
    For existing cert: sudo certbot install --apache --cert-name {domain} -d {domain} -d www.{domain}
    """
    print(
        "\n[5/5 SKIPPED] Certbot — run manually as noted above.\n"
        "   The site is now enabled with snakeoil cert (self-signed). Update with Certbot next."
    )
    # Uncomment below to re-enable automation:
    # print(f"\n[5/5] Running Certbot for {domain}")
    # result = subprocess.run(
    #     [
    #         "certbot",
    #         "--apache",
    #         "-d", domain,
    #         "-d", f"www.{domain}",
    #         "--non-interactive",
    #         "--agree-tos",
    #     ],
    #     text=True,
    # )
    #
    # if result.returncode != 0:
    #     print(
    #         "✖  Certbot failed. The site has been rolled back.\n"
    #         "   Check the certbot logs: journalctl -u certbot  or  /var/log/letsencrypt/letsencrypt.log"
    #     )
    #     _rollback(domain, http_conf, ssl_conf)
    #     sys.exit(1)
    #
    # print(
    #     "✔  Certificate issued — Certbot updated the SSL config (exact template match)"
    # )


# ─── ROLLBACK ────────────────────────────────────────────────────────────────


def _rollback(domain: str, http_conf: str, ssl_conf: str) -> None:
    """
    Undo everything this script did for `domain`.
    Existing sites are never touched by rollback — only files we created.
    """
    print("\n  Rolling back...")
    for conf in [http_conf, ssl_conf]:
        run(["a2dissite", conf], check=False)
        path = os.path.join(SITES_AVAILABLE, conf)
        if os.path.exists(path):
            os.remove(path)
            print(f"  ✔  Removed {path}")
    run(["systemctl", "reload", "apache2"], check=False)
    print("  Rollback complete. All existing sites remain intact.")


# ─── HELPERS ─────────────────────────────────────────────────────────────────


def pick_backend() -> str:
    print("\nWhich backend should handle this site?")
    for key, host in BACKENDS.items():
        print(f"  [{key}] {host}")
    while True:
        choice = input("Enter choice (1/2): ").strip()
        if choice in BACKENDS:
            return BACKENDS[choice]
        print("   Invalid choice, try again.")


# ─── MAIN ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a new reverse-proxy site to VM1 Apache."
    )
    parser.add_argument("domain", nargs="?", help="Domain name, e.g. mysite.com")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("✖  Run as root: sudo python3 add_proxy_site.py")
        sys.exit(1)

    domain = (
        args.domain or input("Enter the domain name (e.g. mysite.com): ").strip()
    ).lower()
    if not domain:
        print("✖  Domain cannot be empty.")
        sys.exit(1)

    backend = pick_backend()
    print(f"\n  Domain  : {domain}")
    print(f"  Backend : {backend}")
    if input("\nProceed? [y/N]: ").strip().lower() != "y":
        print("Aborted.")
        sys.exit(0)

    check_dns(domain)  # 1. DNS check
    check_no_existing_config(domain)  # 2. Safety guard

    print("\n[2/4] Writing Apache config files")
    http_conf = write_http_conf(domain)  # 3. HTTP conf
    ssl_conf = write_ssl_conf(domain, backend)  # 4. SSL conf

    enable_and_reload(domain, http_conf, ssl_conf)  # 5+6. Enable + configtest + reload
    # run_certbot(domain, http_conf, ssl_conf)  # COMMENTED OUT — manual Certbot

    print(
        f"\n🎉  Done! https://{domain} is enabled (with snakeoil cert). Run Certbot manually to add SSL."
    )


# ─── UNIT TESTS ─────────────────────────────────────────────────────────────


class TestAddProxySite(unittest.TestCase):
    def setUp(self):
        self.example_domain = "example.site"
        self.example_backend = "www.temples.bio"
        self.expected_ip = "13.204.103.142"

    @patch("builtins.open")
    def test_write_http_conf_exact_template(self, mock_open):
        # Call the function (it won't actually write, but generates content)
        mock_file = mock_open.return_value.__enter__.return_value
        with patch("os.path.join", return_value="/fake/path.conf"):
            http_conf = write_http_conf(self.example_domain)

        # Capture written content
        written_content = mock_file.write.call_args[0][0]

        expected_content = f"""# Port 80 - redirect HTTP to HTTPS
<VirtualHost *:80>
    ServerName {self.example_domain}
    ServerAlias www.{self.example_domain}

    RewriteEngine On
    RewriteRule ^ https://%{{SERVER_NAME}}%{{REQUEST_URI}} [L,R=301]

    ErrorLog ${{APACHE_LOG_DIR}}/{self.example_domain}-error.log
    CustomLog ${{APACHE_LOG_DIR}}/{self.example_domain}-access.log combined
RewriteCond %{{SERVER_NAME}} ={self.example_domain}
RewriteRule ^ https://%{{SERVER_NAME}}%{{REQUEST_URI}} [END,NE,R=permanent]
</VirtualHost>"""
        self.assertEqual(written_content, expected_content)
        self.assertEqual(http_conf, f"{self.example_domain}.conf")

    @patch("builtins.open")
    def test_write_ssl_conf_exact_template(self, mock_open):
        # Call the function
        mock_file = mock_open.return_value.__enter__.return_value
        with patch("os.path.join", return_value="/fake/path-le-ssl.conf"):
            ssl_conf = write_ssl_conf(self.example_domain, self.example_backend)

        # Capture written content
        written_content = mock_file.write.call_args[0][0]

        expected_content = f'''<IfModule mod_ssl.c>
<VirtualHost *:443>
    ServerName {self.example_domain}

    ErrorLog ${{APACHE_LOG_DIR}}/{self.example_domain}-error.log
    CustomLog ${{APACHE_LOG_DIR}}/{self.example_domain}-access.log combined

    # Reverse proxy
    SSLProxyEngine On
    SSLProxyVerify none
    SSLProxyCheckPeerCN off
    SSLProxyCheckPeerName off

    ProxyPreserveHost Off
    RequestHeader set Host "{self.example_backend}"

    # Forward all requests, preserve the path
    ProxyPass "/" "https://{self.example_backend}/out/{self.example_domain}/"
    ProxyPassReverse "/" "https://{self.example_backend}/out/{self.example_domain}/"

    # Avoid compression issues
    RequestHeader unset Accept-Encoding

    <Directory /var/www/html>
        AllowOverride All
        Require all granted
    </Directory>


    # Certbot will append: Include, ServerAlias, and cert lines here


SSLCertificateFile    /etc/ssl/certs/ssl-cert-snakeoil.pem
SSLCertificateKeyFile /etc/ssl/private/ssl-cert-snakeoil.key
</VirtualHost>
</IfModule>'''
        self.assertEqual(written_content, expected_content)
        self.assertEqual(ssl_conf, f"{self.example_domain}-le-ssl.conf")

    @patch("socket.getaddrinfo")
    def test_check_dns_passes(self, mock_getaddrinfo):
        # Mock successful DNS resolution to expected IP (real getaddrinfo tuple format)
        mock_getaddrinfo.return_value = [
            (
                2,
                1,
                6,
                "",
                ("13.204.103.142", 0),
            ),  # (family, type, proto, canonname, sockaddr)
        ]
        check_dns(self.example_domain)
        mock_getaddrinfo.assert_called_once_with(self.example_domain, None)

    @patch("socket.getaddrinfo")
    def test_check_dns_fails_wrong_ip(self, mock_getaddrinfo):
        # Mock resolution to wrong IP
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("wrong.ip", 0)),
        ]
        with self.assertRaises(SystemExit):
            check_dns(self.example_domain)

    @patch("socket.getaddrinfo")
    def test_check_dns_fails_lookup_error(self, mock_getaddrinfo):
        # Mock DNS lookup failure
        mock_getaddrinfo.side_effect = socket.gaierror("Fake error")
        with self.assertRaises(SystemExit):
            check_dns(self.example_domain)

    @patch("os.path.exists", return_value=False)
    @patch("os.path.join")
    def test_check_no_existing_config_passes(self, mock_join, mock_exists):
        check_no_existing_config(self.example_domain)
        self.assertEqual(mock_exists.call_count, 2)  # Called twice for the two files

    @patch("os.path.exists", side_effect=[True, False])  # Simulate one existing file
    def test_check_no_existing_config_fails(self, mock_exists):
        with self.assertRaises(SystemExit):
            check_no_existing_config(self.example_domain)

    @patch("subprocess.run")
    def test_rollback_cleans_up(self, mock_run):
        # Mock os.path.exists and os.remove
        with (
            patch("os.path.exists", return_value=True),
            patch("os.remove") as mock_remove,
        ):
            _rollback(self.example_domain, "http.conf", "ssl.conf")
        # Verify a2dissite called twice + reload once
        self.assertEqual(mock_run.call_count, 3)
        mock_remove.assert_called()  # Called twice, once per conf


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        unittest.main(argv=[""], exit=False)
    else:
        main()
