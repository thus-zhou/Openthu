# Openthu secure storage and key lifecycle

This module implements the `secure-storage-header-v1` contract for local
credentials, sessions, action journals, indexes, media metadata and event
cursors.  It does not change the existing Windows DPAPI records in place.

## What is protected

- **Cipher:** AES-256-GCM via `cryptography`; no home-grown cipher.
- **Envelope:** 13-byte magic `THU-SECURE-1\0`, a 4-byte big-endian header
  length, a UTF-8 JSON header, then ciphertext plus the GCM tag.
- **AAD:** canonical JSON containing `format_version`, `purpose`,
  `account_domain`, `service_domain`, an opaque `account_ref`, the record
  `revision`, and, for new records, the opaque `record_id`.  The header must
  agree with this AAD, so changing a field without re-authenticating the
  ciphertext fails closed.  Records written before the optional `record_id`
  binding omit it and remain readable only through a legacy store pointer.
- **Nonce:** 12 random bytes per seal operation.  A record sealed twice has two
  different envelopes.  Keys are split by purpose through HKDF.
- **Account reference:** an HMAC-SHA256 pseudonym scoped by purpose, account
  domain and service domain.  The raw account is used only in memory.  A bare
  hash of a low-entropy account is not used as an identifier.

## Key backends

### Windows: current-user DPAPI

`WindowsDpapiKeyProvider` reads a random 32-byte key from a file whose payload
is wrapped by `CryptProtectData` with `CurrentUser` scope.  The key file must be
a regular file, not a symbolic link or reparse point, and its ACL must allow only
the current user (plus local SYSTEM/Administrators as OS administrative
principals).  New files are created only after the parent key directory is
checked/hardened, with inheritance removed and an explicit current-user grant.

This preserves the existing platform boundary: a key wrapped by one Windows
user cannot be read by another user, and no Windows credential is imported into
a cloud checkout.  Legacy `LA-DPAPI-1`, `THU-ACTION-1` and `THU-TODOS-1` files
remain readable only on the matching Windows user through
`LegacyDpapiReader`; they are never silently rewritten.

### Linux/no-desktop: external owner-only file

`FileKeyProvider` uses an external file containing a random 32-byte secret.
The file must be absolute, owned by the current user, mode `0600` (no group or
other permissions), a regular file and not a symbolic link.  Its parent
directory must be owned by the current user and must not be writable by another
user.  A missing key is an error, not an invitation to generate a key beside
the data or to use plaintext.

The key file and the data directory must be separate.  `SecureStore` refuses a
key located inside its data directory.  The data directory is created owner-only
and a symlinked pointer or records directory is rejected.

A desktop keyring can be added later as another explicit provider.  Cloud
operation must not require a graphical session, so no keyring is enabled by
default.

## Atomic records and rotation

`SecureStore` keeps records in an active `records-*` directory.  `store.json`
is the only pointer and is replaced atomically after a file is written and
fsynced.  A failed write removes its temporary file and leaves the previous
record intact.  Every record carries a revision; a caller can require an exact
revision and receives `SECURE_REVISION_CONFLICT` rather than overwriting a
newer record.

`rotate(new_provider)`:

1. reads every active record with its recorded key;
2. re-seals it under the new key while preserving its purpose, domains,
   account reference and revision;
3. immediately unseals the new envelope and compares the plaintext;
4. writes the complete new directory and fsyncs it;
5. switches `store.json` atomically.

A failure before step 5 leaves the old pointer active; a partial new directory
is not read and contains ciphertext only.  The previous directory and key are
retained, so `rollback()` can switch the pointer back after an operator verifies
the old records.  `open_store_from_environment()` can load explicit previous key
files from `THU_SECURE_RETIRED_KEY_FILES` (a JSON list of absolute paths) so a
new process can roll back without relying on an in-memory provider.  Rollback
verifies every previous record before switching the pointer; a corrupt previous
directory is refused.  No automatic cleanup deletes old key material; cleanup is
a manual action after all reads have been verified.

`rotate(new_provider)` also requires a verifiable key location.  A provider whose
`key_path` is missing or inside the data directory is rejected before any target
record is written, so the key/data separation invariant cannot be bypassed by a
rotation.

## Node private-child boundary

`python -m thu_assistant.secure_store.bridge` accepts one bounded JSON request
from stdin and writes one JSON response to stdout.  Actions are limited to
`health`, `inspect`, `seal` and `unseal`.  The key path and data directory come
from `THU_SECURE_KEY_FILE` and `THU_SECURE_DATA_DIR` in a minimal child
environment; they are never accepted from the public MCP surface.  Plaintext
is returned only through this private pipe and never written to logs or MCP
results.  The bridge limits request size to 2 MiB and plaintext to 1 MiB.

## Errors and limits

Missing key, permission failure, unreadable key, malformed envelope, failed
authentication, scope mismatch, record mismatch, revision conflict, missing
record and rotation failure have separate stable codes.  Errors never include
paths, account names, ciphertext or key material.  `health()` authenticates each
record with its provider and counts unreadable/corrupt records instead of
trusting the unkeyed header digest alone; `inspect()` is a header-only view and
must not be used as tamper evidence.  Record reads are bounded by
`MAX_ENVELOPE_BYTES`.

This module does **not** claim:

- protection against a compromised user account, root/administrator, or malware
  running as that user;
- anti-rollback of a whole directory by an attacker who can replace both data
  and pointer (revision catches ordinary concurrent writers, not a full-file
  rollback);
- automatic cross-machine key migration or cloud import of Windows credentials;
- secure deletion of old ciphertext or secure memory wiping;
- authentication of a remote campus service.

## Test expectations

Tests use synthetic accounts and fresh random keys in temporary directories.
They cover nonce uniqueness, tampering of nonce/ciphertext/scope, permission and
symlink rejection, atomic-write failure, interrupted rotation, rollback,
cross-account and cross-purpose read denial, absence of plaintext in stored
files, production refusal without a key, and the frozen JSON header schema.
