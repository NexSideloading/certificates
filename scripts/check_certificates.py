#!/usr/bin/env python3
import os
import requests
from pathlib import Path
from datetime import datetime
import re
import sys

def parse_api_date(date_str):
    """Parse dates like 'Aug 25 01:31:00 2025 GMT' robustly into a datetime.
    Returns a datetime or None if it can't parse.
    """
    if not date_str:
        return None

    # Try a regex to normalize day to zero-padded form (handles single-digit days
    # that may be represented with one space: 'Aug  5 01:31:00 2025 GMT')
    m = re.match(r'([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4})\s+GMT', date_str)
    if not m:
        return None

    month_abbr, day, timepart, year = m.groups()
    day = day.zfill(2)  # ensure two digits
    normalized = f"{month_abbr} {day} {timepart} {year} GMT"

    try:
        return datetime.strptime(normalized, "%b %d %H:%M:%S %Y GMT")
    except ValueError:
        return None

def earliest_of_strings(a_str, b_str):
    """Return the string (a_str or b_str) whose parsed datetime is earlier.
    If both parse, compare datetimes. If only one parses, prefer the parsed one.
    If neither parse, fall back to lexicographical comparison.
    If one is empty, return the other.
    """
    if not a_str and not b_str:
        return ""
    if not a_str:
        return b_str
    if not b_str:
        return a_str

    a_dt = parse_api_date(a_str)
    b_dt = parse_api_date(b_str)

    if a_dt and b_dt:
        return a_str if a_dt < b_dt else b_str
    if a_dt and not b_dt:
        return a_str
    if b_dt and not a_dt:
        return b_str

    # fallback (should be rare): lexical
    return a_str if a_str < b_str else b_str

def latest_of_strings(a_str, b_str):
    """Return the string (a_str or b_str) whose parsed datetime is later.
    Same fallback strategy as earliest_of_strings.
    """
    if not a_str and not b_str:
        return ""
    if not a_str:
        return b_str
    if not b_str:
        return a_str

    a_dt = parse_api_date(a_str)
    b_dt = parse_api_date(b_str)

    if a_dt and b_dt:
        return a_str if a_dt > b_dt else b_str
    if a_dt and not b_dt:
        return a_str
    if b_dt and not a_dt:
        return b_str

    # fallback: lexical
    return a_str if a_str > b_str else b_str

def get_certificate_status(cert_name, no_p12=False):
    """Call your API to get certificate status and parse response.
    
    Args:
        cert_name: Name of the certificate directory or file path
        no_p12: If True, only send mobileprovision file (no P12)
    """
    if no_p12:
        # For no_p12 certificates, cert_name is the full path to the mobileprovision file
        mp_path = Path(cert_name)
        if not mp_path.exists():
            print(f"❌ Missing .mobileprovision file for {cert_name}")
            return None
        mp_files = [mp_path]
        p12_files = []
    else:
        cert_dir = Path(cert_name)
        # Find the .p12 and .mobileprovision files
        p12_files = list(cert_dir.glob("*.p12"))
        mp_files = list(cert_dir.glob("*.mobileprovision"))

        if not mp_files:
            print(f"❌ Missing .mobileprovision file for {cert_name}")
            return None

        mp_path = mp_files[0]

        # For certificates with P12, require it
        if not p12_files:
            print(f"❌ Missing .p12 file for {cert_name}")
            return None

    url = "https://certChecker.novadev.vip/checkCert"

    # Use context manager to ensure files are closed after request
    try:
        if no_p12:
            # Only send mobileprovision file
            with open(mp_path, "rb") as mpf:
                files = {
                    "mobileprovision": (mp_path.name, mpf, "application/octet-stream"),
                }
                data = {}
                response = requests.post(url, files=files, data=data, timeout=60)
        else:
            # Send both P12 and mobileprovision
            p12_path = p12_files[0]
            
            # Read password.txt or use default
            password_file = cert_dir / "password.txt"
            if password_file.exists():
                with open(password_file, 'r', encoding='utf-8') as f:
                    password = f.read().strip()
            else:
                password = "nezushub.vip"

            with open(p12_path, "rb") as p12f, open(mp_path, "rb") as mpf:
                files = {
                    "p12": (p12_path.name, p12f, "application/x-pkcs12"),
                    "mobileprovision": (mp_path.name, mpf, "application/octet-stream"),
                }
                data = {
                    "password": password
                }
                response = requests.post(url, files=files, data=data, timeout=60)
        
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        print(f"❌ Error checking {cert_name}: {e}")
        return None

    # Extract p12 and mobileprovision info
    p12_info = result.get("p12", {})
    mp_info = result.get("mobileprovision", {})

    # Use your logic for status emoji and status value
    # The API extracts the cert from mobileprovision and returns it in the p12 field
    status_raw = p12_info.get("Status", "") or ""
    status_normalized = status_raw.lower()

    if status_normalized == "signed" or status_normalized == "valid":
        final_status = "Valid"
    elif status_normalized == "revoked":
        final_status = "Revoked"
    else:
        final_status = "Unknown"

    # Dates: keep exactly as returned by API
    # Use p12_info dates since the API extracts the cert from mobileprovision
    cert_effective = p12_info.get("Valid From", "")
    cert_expiration = p12_info.get("Valid To", "")
    mp_effective = mp_info.get("Valid From", "")
    mp_expiration = mp_info.get("Valid To", "")

    # Determine actual effective: latest of cert and mp if both exist
    actual_effective = latest_of_strings(cert_effective, mp_effective)

    # Determine actual expiration: earliest of cert and mp if both exist
    actual_expiration = earliest_of_strings(cert_expiration, mp_expiration)

    return {
        "status": final_status,
        "effective": actual_effective,
        "expiration": actual_expiration,
        "company": cert_name,
        "raw": result
    }

def parse_readme_table(readme_content):
    """Parse the markdown tables from README.md.
    
    Returns:
        certificates: List of certificate info dicts with 'no_p12' flag
        lines: The original lines from README
    """
    lines = readme_content.split('\n')
    certificates = []
    
    # Find all table sections
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Check if this is a table header
        if line.startswith('| Certificate | Status |'):
            table_start = i
            no_p12 = False
            
            # Check if this is the "Certificates without P12" section
            # Look backwards to find the heading
            for j in range(max(0, i - 5), i):
                if 'Certificates without P12' in lines[j]:
                    no_p12 = True
                    break
            
            # Parse table rows
            for j in range(table_start + 2, len(lines)):
                row_line = lines[j].rstrip('\n')
                if not row_line.startswith('|') or row_line.startswith('|---'):
                    break

                cells = [cell.strip() for cell in row_line.split('|')[1:-1]]

                if len(cells) >= 3:
                    cert_info = {
                        "company": cells[0],
                        "status": cells[1],
                        "valid_from": cells[2],
                        "valid_to": cells[3] if len(cells) > 3 else "",
                        "line_index": j,
                        "no_p12": no_p12
                    }
                    certificates.append(cert_info)
        
        i += 1

    return certificates, lines

def sort_certificates_by_expiry(certificates):
    """Sort certificates by status (valid first) then by expiry date (valid_to field) in ascending order.
    
    Args:
        certificates: List of certificate info dicts with 'valid_to' and 'status' fields
        
    Returns:
        Sorted list of certificates
    """
    def get_sort_key(cert):
        # Primary sort: status (valid/signed first, then others)
        status = cert.get('status', '').lower()
        is_valid = status in ('valid', 'signed')
        status_priority = 0 if is_valid else 1
        
        # Secondary sort: expiry date
        valid_to = cert.get('valid_to', '')
        dt = parse_api_date(valid_to)
        if dt:
            expiry_key = dt
        else:
            # If parsing fails, use a far future date so it sorts last
            expiry_key = datetime.max
        
        return (status_priority, expiry_key)
    
    return sorted(certificates, key=get_sort_key)

def update_readme_table(certificates, lines):
    """Update the README.md lines with new certificate statuses and sort by expiry."""
    updated_lines = lines.copy()

    # Separate certificates by table (with P12 vs without P12)
    certs_with_p12 = [c for c in certificates if not c.get('no_p12', False)]
    certs_without_p12 = [c for c in certificates if c.get('no_p12', False)]

    # Sort each group by expiry date
    sorted_with_p12 = sort_certificates_by_expiry(certs_with_p12)
    sorted_without_p12 = sort_certificates_by_expiry(certs_without_p12)

    # Update each table section
    _update_table_section(sorted_with_p12, updated_lines, False)
    _update_table_section(sorted_without_p12, updated_lines, True)

    return updated_lines

def _update_table_section(sorted_certs, lines, is_no_p12_table):
    """Update a specific table section with sorted certificates.
    
    Args:
        sorted_certs: List of sorted certificate info dicts
        lines: The README lines to update
        is_no_p12_table: True if this is the "Certificates missing P12" table
    """
    if not sorted_certs:
        return

    # Find the table section
    table_start = None
    table_end = None
    
    for i, line in enumerate(lines):
        if line.startswith('| Certificate | Status |'):
            # Check if this is the correct table
            if is_no_p12_table:
                # Look backwards for "Certificates missing P12" heading
                is_correct = False
                for j in range(max(0, i - 5), i):
                    if 'Certificates missing P12' in lines[j]:
                        is_correct = True
                        break
                if not is_correct:
                    continue
            else:
                # Look backwards for "Certificates" heading (not "missing P12")
                is_correct = True
                for j in range(max(0, i - 5), i):
                    if 'Certificates missing P12' in lines[j]:
                        is_correct = False
                        break
                if not is_correct:
                    continue
            
            table_start = i
            # Find the end of the table
            for j in range(i + 2, len(lines)):
                if not lines[j].startswith('|') or lines[j].startswith('|---'):
                    table_end = j
                    break
            break

    if table_start is None or table_end is None:
        return

    # Build new table rows
    header_line = lines[table_start]
    separator_line = lines[table_start + 1]
    
    new_rows = [header_line, separator_line]
    
    for cert in sorted_certs:
        status = cert.get('status', '').lower()
        status_emoji = '✅' if status == 'valid' else ('❌' if status == 'revoked' else '⚠️')

        # Compose new status cell text with emoji and status word
        if status == 'valid':
            new_status = f"{status_emoji} Signed"
        elif status == 'revoked':
            new_status = f"{status_emoji} Revoked"
        elif status == 'unknown':
            new_status = f"{status_emoji} Status: Unknown"
        else:
            new_status = cert.get('status', '')

        valid_from = cert.get('valid_from', '').strip()
        valid_to = cert.get('valid_to', '').strip()

        # Build the new row
        new_row = f"| {cert['company']} | {new_status} | {valid_from} | {valid_to} |"
        new_rows.append(new_row)

    # Replace the table section in lines
    # Remove old table rows (from table_start to table_end)
    # Insert new rows at table_start
    lines[table_start:table_end] = new_rows

def main():
    # Read README.md
    try:
        with open('README.md', 'r', encoding='utf-8') as f:
            readme_content = f.read()
    except FileNotFoundError:
        print("README.md not found")
        sys.exit(1)

    certificates, lines = parse_readme_table(readme_content)

    if not certificates:
        print("No certificates found in README.md")
        return

    print(f"Found {len(certificates)} certificates in README.md")

    updated_certs = []
    for cert_info in certificates:
        company = cert_info['company']
        no_p12 = cert_info.get('no_p12', False)
        print(f"Checking {company}{' (no P12)' if no_p12 else ''}...")

        # For no_p12 certificates, they are files in the 'No P12' directory with .mobileprovision extension
        cert_path = f"No P12/{company}.mobileprovision" if no_p12 else company
        result = get_certificate_status(cert_path, no_p12=no_p12)
        if result:
            cert_info['status'] = result['status']
            cert_info['valid_from'] = result['effective']
            cert_info['valid_to'] = result['expiration']
            updated_certs.append(cert_info)

            status_emoji = '✅' if result['status'] == 'Valid' else ('❌' if result['status'] == 'Revoked' else '⚠️')
            print(f"  {status_emoji} Status: {result['status']}")
            print(f"  📅 Actual Effective: {result['effective']}")
            print(f"  📅 Actual Expiry: {result['expiration']}")
        else:
            print(f"  ⚠️ Could not check status")
            updated_certs.append(cert_info)

    updated_lines = update_readme_table(updated_certs, lines)

    with open('README.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(updated_lines))

    print("\n✅ README.md updated successfully!")

if __name__ == "__main__":
    main()
