# Sovereign Ethiopian Hosting & Infrastructure Specification
**Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology, Addis Ababa, Ethiopia**  
*Document Version:* 1.0.0 | *Effective Date:* October 2026  
*Status:* **Approved for Production Pilot Deployment**

---

## 1. Regulatory Context & Sovereign Data Residency Mandate (Rule 7)

In strict adherence to **Rule 7 (Sovereign Data Residency)** of Daily Close and the regulatory frameworks established by the **National Bank of Ethiopia (NBE)**, the **Information Network Security Administration (INSA)** (Proclamation No. 808/2013), and the **Computer Crime Proclamation No. 958/2016**, all transactional, financial, and customer data managed by Daily Close must remain physically hosted and processed within the sovereign borders of the Federal Democratic Republic of Ethiopia.

> [!IMPORTANT]
> **Sovereign Residency Guarantee:**
> Under no circumstances may wholesale customer databases, open sales invoices, payment statement lines, cryptographic master keys, or audit logs be hosted on, proxied through, or backed up to cloud servers outside Ethiopian territorial jurisdiction.

---

## 2. Certified Domestic Hosting Facilities

Daily Close is provisioned exclusively across certified domestic Tier III data center facilities in Addis Ababa:

### Primary Hosting Facility: Wingu Africa (Addis Ababa One)
- **Location:** Addis Ababa ICT Park, Goro Road, Bole Sub-City, Addis Ababa, Ethiopia.
- **Classification:** Carrier-neutral Tier III certified data center.
- **Power Resilience:** Dual-feed 15kV utility grid feeds, N+1 Cummins diesel generators with 72-hour on-site fuel reserve, and dual redundant UPS systems (A+B power paths).
- **Network Connectivity:** Redundant underground dark fiber connections terminating directly on the **Ethiopian Internet Exchange (EthIX)**, with direct interconnects to **Ethio Telecom** and **Safaricom Telecommunications Ethiopia**.
- **Physical Security:** Multi-layered biometric access control, 24/7 armed perimeter security, mantraps, and tamper-evident CCTV surveillance with 90-day sovereign archival retention.

### Secondary Disaster Recovery Facility: Ethio Telecom Sovereign Cloud
- **Location:** Ethio Telecom Incode Data Center, Bole, Addis Ababa, Ethiopia.
- **Role:** Asynchronous disaster recovery site for encrypted, offline `age` backup storage.

---

## 3. Sovereign Domain & Domestic Transit Architecture

### Sovereign ccTLD Registration (.et)
- **Primary Domain:** `dailyclose.birana.et`
- **ccTLD Registry:** Ethiopian Telecommunications Corporation / EthERNet.
- **DNS Hosting:** Dual authoritative DNS servers located physically within Ethiopia (`ns1.birana.et`, `ns2.birana.et`), with DNSSEC enabled to prevent DNS spoofing or route hijacking.

### Domestic Peering via EthIX
- **Local Transit Guarantee:** All HTTP/HTTPS traffic between wholesale merchant client browsers, field sales agents in Merkato, and the Daily Close application server peers through EthIX in Addis Ababa.
- **Latency & Reliability:** Sub-10ms round-trip latency within Addis Ababa, eliminating reliance on international undersea fiber links (e.g. Djibouti / Mombasa landing stations) for core wholesale accounting.

---

## 4. Zero Foreign Telemetry & Dependency Isolation

Daily Close is engineered to operate in a completely air-gapped or domestic-only network posture:

1. **Zero Third-Party Cloud SaaS:**
   - No external error tracking services (e.g. Sentry Cloud, Rollbar). Errors are logged locally to structured JSON files with log rotation.
   - No third-party analytics (e.g. Google Analytics, Mixpanel, Segment).
2. **Zero External CDN Dependencies:**
   - All static assets (HTMX, CSS frameworks, inline SVGs, and locale files) are served strictly from `'self'` via Nginx.
   - All Google Fonts or external CDNs are blocked via the strict Content Security Policy (CSP):
     ```http
     Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none';
     ```
3. **Local Timekeeping:**
   - Local NTP time synchronization configured against Ethio Telecom domestic stratum-1 NTP servers (`ntp.telecom.net.et`).

---

## 5. Host Operating System & Hardening Standards

Production servers run on minimal Ubuntu 22.04 LTS (Jammy Jellyfish) or Debian 12 (Bookworm) minimal sovereign images:
- **Kernel Hardening:** `sysctl` security parameters enabled (ASLR, SYN cookies, IP spoofing protection, ICMP redirect rejection).
- **Firewall (UFW / iptables):**
  - Port `80/tcp`: HTTP (Permanent 301 redirect to HTTPS).
  - Port `443/tcp`: HTTPS (TLS 1.2 / TLS 1.3 only).
  - Port `22/tcp`: SSH restricted to private management VPN with Ed25519 hardware key authentication.
  - Port `5432/tcp`: PostgreSQL bound strictly to `127.0.0.1` (no external interface exposure).
- **PostgreSQL Least Privilege:** Unprivileged application runtime under role `app` (`NOBYPASSRLS`), with database Row-Level Security (RLS) active across all tenant tables.

---

## 6. Verification Checklist for Infrastructure Gate

| Verification Item | Requirement | Verification Method | Status |
| :--- | :--- | :--- | :---: |
| **Physical Location** | Wingu Africa / Ethio Telecom (Addis Ababa) | Colocation contract & IP WHOIS | **VERIFIED** |
| **ccTLD Domain** | Registered under `.et` namespace | `whois birana.et` / DNS lookup | **VERIFIED** |
| **Domestic Transit** | Route resolves via EthIX | `traceroute` / BGP route audit | **VERIFIED** |
| **Zero Foreign CDN** | No external domains in CSP | Nginx audit (`test_nginx_tls_and_security_headers`) | **VERIFIED** |
| **Database Binding** | PostgreSQL bound strictly to localhost | `netstat -tlpn` / socket inspection | **VERIFIED** |
