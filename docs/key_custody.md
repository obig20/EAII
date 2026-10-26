# Disaster Recovery Key Custody Plan (Age Private Key)
**Project:** Daily Close — [COMPANY_LEGAL_NAME] (Addis Ababa, Ethiopia)  
**Target Operator:** Solo Founder / Sole Technical Custodian (`[FOUNDER_NAME]`)  
**Security Standard:** Asymmetric Disaster Recovery Encryption (`age` v1.0+)

---

## 1. Threat Model & Principles

Daily Close stores sensitive financial transactions, invoice balances, and payment records for wholesalers and distributors.
- **Threat:** If the production application server is compromised, attackers must not be able to decrypt past database backups.
- **Principle:** Asymmetric public-key encryption. The production server possesses **only** the public recipient key (`deploy/backup_pubkey.txt`).
- **Core Invariant:** The private decryption identity (`AGE-SECRET-KEY-1...`) is generated offline and **never touches** the production application server, any cloud disk volume, or git repositories.

---

## 2. Key Generation Procedure

The key must be generated once by the founder (`[FOUNDER_NAME]`) on an offline workstation (e.g. laptop with Wi-Fi disabled):

1. **Generate the Keypair:**
   ```bash
   age-keygen -o dailyclose_recovery_identity.txt
   ```
2. **Extract Public Key:**
   The output displays:
   - **Public Recipient:** `age1...` -> Commit this to `deploy/backup_pubkey.txt`.
   - **Private Identity:** `AGE-SECRET-KEY-1...` -> Must **never** be committed or copied to the server.
3. **Delete Plaintext from Workstation:** Once offline backups and paper copies are secured, remove `dailyclose_recovery_identity.txt` from the operating system drive (`shred` or secure erase).

---

## 3. Solo-Founder Custody & Storage

As a solo founder, elaborate dual-officer physical ceremonies are unrealistic and introduce fragile failure modes. Instead, custody relies on defense-in-depth, encrypted physical media, and legal/bank escrow:

### Primary Copy (Founder's Encrypted Flash Drive)
- Stored on an encrypted USB flash drive (BitLocker, LUKS, or VeraCrypt) protected by a strong passphrase.
- Kept in the personal possession of `[FOUNDER_NAME]` (stored alongside personal vital documents).

### Secondary Copy (Legal or Bank Escrow — Choose One or Both)
- **Option A: Bank Safety Deposit Box**
  - Sealed tamper-evident envelope containing a second encrypted flash drive and printed paper key.
  - Deposited at `[ESCROW_BANK_BRANCH]` (e.g., Commercial Bank of Ethiopia or Awash Bank branch in Addis Ababa).
  - Escrow release instructions: Accessible only by `[FOUNDER_NAME]` or, in the event of incapacitation/death, authorized legal representative.
- **Option B: Attorney / Notary Escrow**
  - Sealed tamper-evident envelope held by company legal counsel (`[PRIMARY_LAWYER_OR_NOTARY]`).
  - Signed instructions authorizing release solely upon presentation of legal succession documentation.

### Cold Paper Backup
- A printed copy of the `AGE-SECRET-KEY-1...` identity string and QR code.
- Stored in a sealed, waterproof tamper-evident envelope in the founder's home fireproof lockbox.

---

## 4. Disaster Recovery & Restoration Procedure

In the event of database corruption, ransomware, or server rebuild:

1. Provision a clean Ubuntu server and clone the repository.
2. Retrieve the private identity file from the founder's primary flash drive:
   ```bash
   export IDENTITY_FILE="/path/to/dailyclose_recovery_identity.txt"
   ```
3. Transfer the latest encrypted backup (`.sql.age`) to the restoration host.
4. Execute the automated restore script:
   ```bash
   ./deploy/restore_backup.sh /path/to/backup.sql.age
   ```
5. `restore_backup.sh` decrypts the backup in memory directly into `psql`, validates record counts and schema integrity, and leaves no unencrypted dump file on disk.
6. Unmount the recovery media immediately and securely wipe any temporary files.

---

## 5. Maintenance & Annual Review

- **Semi-Annual Restore Drill:** Every six months, `[FOUNDER_NAME]` performs a test restoration on a local scratch database to verify that the private key successfully decrypts current nightly backups.
- **Key Rotation:** The keypair is rotated every 12 months, or immediately upon suspicion of compromise. Archived private keys must be preserved offline for historical backup decryption as required by Ethiopian tax and accounting record-retention laws (typically 5 to 7 years).

---

### Custodian Information
- **Sole Custodian:** `[FOUNDER_NAME]`
- **Primary Contact:** `[FOUNDER_PHONE]` / `[FOUNDER_EMAIL]`
- **Legal Representative:** `[PRIMARY_LAWYER_OR_NOTARY]`
- **Escrow Institution:** `[ESCROW_BANK_BRANCH]`
