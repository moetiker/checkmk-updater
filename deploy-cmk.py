#!/usr/bin/env python3
"""Parallel deployment of Checkmk updates across multiple hosts.

Detects the OS and current site versions on each host via SSH, downloads
the matching packages from download.checkmk.com, and updates every site
within its own major version. All hosts are updated in parallel; the
central site host is always updated last. See README.md for setup and usage.
"""
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "hosts.json")
STABLE_URL = "https://download.checkmk.com/stable_downloads.json"
PKG_EXTENSIONS = (".deb", ".rpm")

# Supported distro keys (must match keys in Checkmk download JSON)
SUPPORTED_DISTROS = frozenset({
    "focal", "jammy", "noble", "resolute",        # Ubuntu 20.04/22.04/24.04/26.04
    "buster", "bullseye", "bookworm",             # Debian 10/11/12
    "el7", "el8", "el9",                          # RHEL/CentOS/Rocky/Alma
    "sles12sp5", "sles15sp3", "sles15sp4",        # SLES
    "sles15sp5", "sles15sp6",
})


def load_config():
    """Load host/site config from hosts.json. Returns (hosts_dict, central_host, central_sites)."""
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    return config["hosts"], config["central"]["host"], config["central"]["sites"]


def load_credentials():
    """Read download credentials from .pass file. Returns (user, password)."""
    pass_file = os.path.join(SCRIPT_DIR, ".pass")
    with open(pass_file) as f:
        lines = f.read().splitlines()
    return lines[0], lines[1]


# omd uses long edition names, download JSON uses short keys
EDITION_MAP = {
    "cee": "cee",
    "cme": "cme",
    "cre": "cre",
    "cce": "cce",
    "enterprise": "cee",
    "pro": "cee",
    "managed": "cme",
    "ultimatemt": "cme",
    "raw": "cre",
    "community": "cre",
    "cloud": "cce",
    "ultimate": "cce",
}


def parse_version(ver):
    """Parse a CMK version string into (major, edition_key).
    '2.4.0p25.cee' -> ('2.4.0', 'cee')
    '2.5.0b1.ultimatemt' -> ('2.5.0', 'cme')
    """
    m = re.match(r"(\d+\.\d+\.\d+)\S*\.(\w+)$", ver)
    if not m:
        return None, None
    major = m.group(1)
    edition_raw = m.group(2)
    edition = EDITION_MAP.get(edition_raw, edition_raw)
    return major, edition


def get_all_versions():
    """Fetch all available versions (stable + oldstable + beta) from Checkmk.
    Returns dict: (major, edition_key) -> (full_version, {os_name: pkg_filename})
    """
    with urllib.request.urlopen(STABLE_URL) as resp:
        data = json.loads(resp.read())
    versions = {}
    for entry in data["checkmk"].values():
        if entry.get("class") in ("stable", "oldstable", "beta"):
            version = entry["version"]
            m = re.match(r"(\d+\.\d+\.\d+)", version)
            if not m:
                continue
            major = m.group(1)
            for edition_key, edition_data in entry.get("editions", {}).items():
                pkgs = {os_name: files[0] for os_name, files in edition_data.items()}
                versions[(major, edition_key)] = (version, pkgs)
    if not versions:
        sys.exit("No versions found")
    return versions


def detect_distro(host):
    """Detect distro on a remote host. Returns (distro_key, pkg_type) or (None, None) on failure."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", host, "cat /etc/os-release"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, None
    info = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            info[key] = val.strip('"')
    distro_id = info.get("ID", "")
    version_id = info.get("VERSION_ID", "")
    codename = info.get("VERSION_CODENAME", "")
    if distro_id in ("ubuntu", "debian"):
        return codename, "deb"
    if distro_id in ("rhel", "centos", "rocky", "almalinux"):
        major = version_id.split(".")[0]
        return f"el{major}", "rpm"
    if distro_id in ("sles", "suse"):
        parts = version_id.split(".")
        if len(parts) == 2:
            return f"sles{parts[0]}sp{parts[1]}", "rpm"
        return f"sles{parts[0]}", "rpm"
    return None, None


def cleanup_old_packages(keep):
    """Remove old .deb/.rpm files, keeping only the ones in the keep set."""
    for f in os.listdir(SCRIPT_DIR):
        if any(f.endswith(ext) for ext in PKG_EXTENSIONS) and f not in keep:
            path = os.path.join(SCRIPT_DIR, f)
            print(f"  Removing old package: {f}")
            os.remove(path)


def download_pkg(version, pkg, user, password, dry_run=False):
    """Download a package if it doesn't exist yet."""
    pkg_path = os.path.join(SCRIPT_DIR, pkg)
    if os.path.exists(pkg_path):
        print(f"  {pkg} already downloaded.")
        return True
    if dry_run:
        print(f"  Would download: {pkg}")
        return True
    url = f"https://download.checkmk.com/checkmk/{version}/{pkg}"
    print(f"  Downloading {pkg} ...")
    # Write credentials to temp file to avoid exposing password in process list
    with tempfile.NamedTemporaryFile(mode="w", suffix=".wgetrc", delete=False) as tmp:
        tmp.write(f"user={user}\npassword={password}\n")
        wgetrc = tmp.name
    try:
        env = {**os.environ, "WGETRC": wgetrc}
        result = subprocess.run(
            ["wget", "--progress=bar:force", url, "-O", pkg_path],
            env=env,
        )
    finally:
        os.remove(wgetrc)
    if result.returncode != 0:
        # Remove partial/corrupt file
        try:
            os.remove(pkg_path)
        except FileNotFoundError:
            pass
        print(f"  Download FAILED: {pkg}")
        return False
    print(f"  Download complete: {pkg}")
    return True


print_lock = threading.Lock()


class HostLogger:
    """Per-host logger that writes to console (with prefix) and log file."""

    def __init__(self, host, log_dir):
        self.host = host
        self.log_file = open(os.path.join(log_dir, f"{host}.log"), "w")

    def log(self, msg):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_file.write(f"[{timestamp}] {msg}\n")
        self.log_file.flush()
        with print_lock:
            print(f"  [{self.host}] {msg}")

    def close(self):
        self.log_file.close()


def run(cmd, logger):
    """Run a command, stream output line by line to logger."""
    logger.log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            logger.log(f"  {line}")
    proc.wait()
    if proc.returncode != 0:
        logger.log(f"FAILED (exit {proc.returncode})")
        return False
    return True


def check_version(host, site):
    """Return the current version of a site, or None on error."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", host, f"sudo omd sites {site}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == site:
            return parts[1]
    return None


def probe_host(host, sites):
    """Detect distro and current CMK versions on a host."""
    distro_key, pkg_type = detect_distro(host)
    site_versions = {site: check_version(host, site) for site in sites}
    return host, distro_key, pkg_type, site_versions


def deploy(host, sites, versions_map, site_versions, pkg_type, log_dir, dry_run=False):
    """Deploy updates to all sites on a host.
    versions_map: {site -> (target_omd_version, pkg_filename)}
    site_versions: {site -> current_version} from probe phase
    Returns (ok, site_results) where site_results is a list of
    (site, old_version, new_version, status, duration_seconds).
    """
    host_start = time.monotonic()
    logger = HostLogger(host, log_dir)
    site_results = []
    try:
        # Determine which sites need updating
        updates = {}
        for site in sites:
            current = site_versions.get(site)
            if not current:
                logger.log(f"[{site}] Could not detect version, skipping.")
                site_results.append((site, current, None, "SKIPPED", 0))
                continue
            if site not in versions_map:
                logger.log(f"[{site}] No update available for {current}, skipping.")
                site_results.append((site, current, current, "SKIPPED", 0))
                continue
            target_version, pkg = versions_map[site]
            if current == target_version:
                logger.log(f"[{site}] Already on {target_version}, skipping.")
                site_results.append((site, current, current, "OK", 0))
            else:
                logger.log(f"[{site}] {current} -> {target_version}")
                updates[site] = (target_version, pkg)

        if not updates:
            return True, site_results

        # Determine unique packages to install
        pkgs_to_install = set(pkg for _, pkg in updates.values())

        if pkg_type == "rpm":
            install_tpl = "sudo rpm -Uvh /tmp/{}"
        else:
            install_tpl = "sudo dpkg -i /tmp/{}"

        # Check which versions are already installed
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", host, "sudo omd versions"],
            capture_output=True, text=True,
        )
        installed = set(result.stdout.splitlines()) if result.returncode == 0 else set()

        # Copy and install only missing packages
        for pkg in pkgs_to_install:
            target_version = next(ver for ver, p in updates.values() if p == pkg)
            if target_version in installed:
                logger.log(f"{target_version} already installed, skipping package install.")
                continue
            if dry_run:
                logger.log(f"Would install: {pkg}")
                continue
            pkg_path = os.path.join(SCRIPT_DIR, pkg)
            if not run(["scp", pkg_path, f"{host}:/tmp/{pkg}"], logger):
                for site, (tv, _) in updates.items():
                    site_results.append((site, site_versions.get(site), None, "FAILED", 0))
                return False, site_results
            logger.log(f"Installing {pkg} ...")
            if not run(["ssh", host, install_tpl.format(pkg)], logger):
                for site, (tv, _) in updates.items():
                    site_results.append((site, site_versions.get(site), None, "FAILED", 0))
                return False, site_results

        if dry_run:
            for site, (target_version, _) in updates.items():
                logger.log(f"Would run: sudo omd stop {site}")
                logger.log(f"Would run: sudo omd -f -V {target_version} update --conflict=install {site}")
                logger.log(f"Would run: sudo omd start {site}")
                site_results.append((site, site_versions.get(site), target_version, "DRY-RUN", 0))
            return True, site_results

        # Update each site with its matching version
        ok = True
        for site, (target_version, _) in updates.items():
            site_start = time.monotonic()
            site_ok = True
            logger.log(f"[{site}] Stopping site ...")
            if not run(["ssh", host, f"sudo omd stop {site}"], logger):
                site_ok = False
            if site_ok:
                logger.log(f"[{site}] Updating site ...")
                if not run(["ssh", host, f"sudo omd -f -V {target_version} update --conflict=install {site}"], logger):
                    site_ok = False
            if site_ok:
                logger.log(f"[{site}] Starting site ...")
                if not run(["ssh", host, f"sudo omd start {site}"], logger):
                    site_ok = False
            duration = time.monotonic() - site_start
            if site_ok:
                site_results.append((site, site_versions.get(site), target_version, "OK", duration))
            else:
                ok = False
                site_results.append((site, site_versions.get(site), None, "FAILED", duration))

        # Cleanup uploaded packages - not critical
        for pkg in pkgs_to_install:
            run(["ssh", host, f"rm -f /tmp/{pkg}"], logger)
        # Only remove old versions if ALL site updates succeeded
        if ok:
            logger.log("Removing old versions ...")
            run(["ssh", host, "sudo omd cleanup"], logger)
        else:
            logger.log("Skipping omd cleanup due to failed updates.")
        logger.log("Done.")
        return ok, site_results
    finally:
        logger.close()


def fmt_duration(seconds):
    """Format seconds into a human-readable string like '2m 35s'."""
    if seconds < 1:
        return "-"
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def print_report(report_data, all_hosts):
    """Print a summary table showing per-host/site results."""
    print("\n" + "=" * 70)
    print("  SUMMARY REPORT")
    print("=" * 70 + "\n")

    # Process hosts in config order
    for host in all_hosts:
        if host not in report_data:
            continue
        host_duration, site_results = report_data[host]
        print(f"  {host}  ({fmt_duration(host_duration)})")
        if not site_results:
            print(f"    (no site data)")
        for site, old_ver, new_ver, status, site_dur in site_results:
            old_str = old_ver or "?"
            new_str = new_ver or old_str
            dur_str = fmt_duration(site_dur)
            if status == "OK" and old_ver != new_ver:
                print(f"    {site:20s}  {old_str} -> {new_str}  {status}  {dur_str}")
            elif status == "FAILED":
                print(f"    {site:20s}  {old_str}  {status}  {dur_str}")
            elif status == "DRY-RUN":
                print(f"    {site:20s}  {old_str} -> {new_str}  {status}")
            elif status == "SKIPPED":
                print(f"    {site:20s}  {old_str}  {status}")
            else:
                print(f"    {site:20s}  {new_str}  {status}")
        print()

    print("=" * 70 + "\n")


def main():
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    if dry_run:
        print("=== DRY RUN ===\n")

    host_sites, central_host, central_sites = load_config()

    # Load credentials early to fail fast
    user, password = None, None
    if not dry_run:
        try:
            user, password = load_credentials()
        except FileNotFoundError:
            sys.exit("Error: .pass file not found")

    all_versions = get_all_versions()

    # Create log directory for this run
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = os.path.join(LOG_DIR, timestamp)
    os.makedirs(log_dir, exist_ok=True)
    print(f"Logs: {log_dir}\n")

    # Detect distro and site versions on all hosts in parallel
    all_hosts = {**host_sites, central_host: central_sites}
    host_info = {}    # host -> (os_name, pkg_type, site_versions)
    errors = []
    print("--- Detecting OS and site versions ---\n")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(probe_host, host, sites): host
            for host, sites in all_hosts.items()
        }
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            try:
                _, distro_key, pkg_type, site_versions = future.result()
            except Exception as exc:
                print(f"  [{host}] Error: {exc}")
                errors.append(host)
                continue
            if distro_key is None:
                print(f"  [{host}] SSH connection failed")
                errors.append(host)
                continue
            if distro_key not in SUPPORTED_DISTROS:
                print(f"  [{host}] Unsupported distro: {distro_key}")
                errors.append(host)
                continue
            host_info[host] = (distro_key, pkg_type, site_versions)
            versions = ", ".join(f"{s}={v or '?'}" for s, v in site_versions.items())
            print(f"  [{host}] {distro_key}, {versions}")

    if errors:
        print(f"\nAborting: failed to probe {', '.join(errors)}")
        sys.exit(1)

    print()

    # Build per-host version map: {site -> (target_omd_version, pkg)}
    # and collect all packages we need to download
    all_needed_pkgs = {}  # pkg_filename -> download_version
    host_versions_map = {}  # host -> {site -> (omd_version, pkg)}
    for host, (os_name, pkg_type, site_versions) in host_info.items():
        vmap = {}
        for site, current in site_versions.items():
            if not current:
                continue
            major, edition = parse_version(current)
            if not major or not edition:
                continue
            key = (major, edition)
            if key not in all_versions:
                continue
            version, pkgs_by_os = all_versions[key]
            if os_name not in pkgs_by_os:
                print(f"  [{host}] No {os_name} package for {major} ({edition})")
                sys.exit(1)
            pkg = pkgs_by_os[os_name]
            # Use edition suffix from current version (omd knows it by that name)
            current_edition_suffix = current.split(".")[-1]
            omd_version = f"{version}.{current_edition_suffix}"
            vmap[site] = (omd_version, pkg)
            all_needed_pkgs[pkg] = version
        host_versions_map[host] = vmap

    # Show planned updates
    print("--- Planned updates ---\n")
    any_update = False
    for host, vmap in host_versions_map.items():
        site_versions = host_info[host][2]
        for site, (target, _) in vmap.items():
            current = site_versions.get(site, "?")
            if current != target:
                print(f"  [{host}] {site}: {current} -> {target}")
                any_update = True
            else:
                print(f"  [{host}] {site}: {current} (up to date)")
    if not any_update:
        print("  Nothing to update.")
        print("\nAll done.")
        return
    print()

    # Download needed packages and remove old ones
    print(f"--- Downloading packages ({len(all_needed_pkgs)}) ---\n")
    if not dry_run:
        cleanup_old_packages(set(all_needed_pkgs.keys()))
    for pkg, version in all_needed_pkgs.items():
        if not download_pkg(version, pkg, user, password, dry_run):
            sys.exit(1)
    print()

    # Update all hosts in parallel (except the central site host)
    failed = []
    # Collect results: host -> (host_duration, site_results)
    report_data = {}
    print("--- Updating all sites in parallel ---\n")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {}
        host_start_times = {}
        for host in host_sites:
            host_start_times[host] = time.monotonic()
            futures[executor.submit(
                deploy, host, host_sites[host], host_versions_map[host],
                host_info[host][2], host_info[host][1], log_dir, dry_run,
            )] = host
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            host_duration = time.monotonic() - host_start_times[host]
            try:
                ok, site_results = future.result()
                report_data[host] = (host_duration, site_results)
                if not ok:
                    failed.append(host)
            except Exception as exc:
                print(f"  [{host}] Unexpected error: {exc}")
                report_data[host] = (host_duration, [])
                failed.append(host)

    # The central site host is always updated last, separately
    print("\n--- Updating central site host ---\n")
    central_start = time.monotonic()
    try:
        ok, site_results = deploy(central_host, central_sites, host_versions_map[central_host],
                                  host_info[central_host][2], host_info[central_host][1],
                                  log_dir, dry_run)
        report_data[central_host] = (time.monotonic() - central_start, site_results)
        if not ok:
            failed.append(central_host)
    except Exception as exc:
        print(f"  [{central_host}] Unexpected error: {exc}")
        report_data[central_host] = (time.monotonic() - central_start, [])
        failed.append(central_host)

    # Print summary report
    print_report(report_data, all_hosts)

    if failed:
        print(f"FAILED: {', '.join(failed)}")
        print(f"Check logs: {log_dir}")
        sys.exit(1)
    print("All done.")


if __name__ == "__main__":
    main()
