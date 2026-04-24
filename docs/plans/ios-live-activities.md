# Proposal: native iOS companion — Live Activities, E2EE push, actionable approvals

> Status: proposal / spec. No code in this pass. Archived from
> `~/.claude/plans/okay-i-want-to-zazzy-bentley.md`. Companion to
> `plugin-marketplace.md`; both can ship independently.

## 1. Context & goals

v0.1 of `agent-notify` forwards agent events to Slack/Discord. That works, but
three things are structurally out of reach on those sinks:

- **Lock-screen actionability.** Slack/Discord actions require opening the app,
  logging in, and tapping through. For an approve/deny prompt that shouldn't
  need more than a glance + biometric, the fastest native path is iOS's own
  notification action buttons.
- **At-a-glance in-flight state.** Live Activities on the Lock Screen / Dynamic
  Island are the right UX for "Claude is working, here's the current step".
  Nothing on Slack looks like that.
- **Payload privacy from the push operator.** Apple APNs sees every Slack
  webhook body today (because Slack itself sends us a formatted push). A native
  sink with a Notification Service Extension (NSE) lets us put ciphertext on
  the wire and decrypt only on the device.

**Goals of this doc:** pre-scope the architecture, crypto, and threat model
tightly enough that a future implementation pass can start at step 1 of the
prototyping order without having to re-derive these decisions.

**Non-goals:**

- Multi-user / team features. This is a single-user tool; one paired iPhone
  per Mac.
- A cloud relay. Apple APNs stays in the picture (unavoidable — see §2), but
  we run no other server infra.
- Replacing the Slack/Discord sinks. iOS is additive; Slack/Discord remain the
  easy default for users who don't want to ship an iOS app.
- Android. Explicit non-goal; architecture below is iOS-specific.

## 2. High-level architecture

```
Claude Code / Codex
    │ hook
    ▼
agent-notify  (Mac; existing)
    ├── IOSSink  ─── APNs (Apple) ─── iPhone NSE ── decrypt ── UserNotification
    │                                         │
    │   Tailscale ◄──── (optional fetch) ─────┘
    │                                         ▲
    └── PendingApprovals (new) ◄─── HTTPS over Tailscale ─── iOS app (action response)
         ▲                                                          │
         │ unblocks                                                  │ "approve"/"deny"
         └── PreToolUse hook (blocks here)
```

Two observations that drive the whole design:

1. **APNs is unavoidable.** Tailscale keeps the iPhone reachable on the tailnet
   even when it's backgrounded, but it cannot *wake a suspended iOS app*. Only
   an APNs push can wake the device process. So "no server infra" means
   "we still use Apple's APNs, but we run nothing else". The question becomes
   only how much content goes *in* the APNs payload vs. fetched post-wake from
   the Mac over Tailscale.
2. **Approve/deny is real, not cosmetic.** Claude Code's `PreToolUse` hook can
   block up to its configured `timeout` (default 600 s, overridable to ~1 h)
   and return a JSON object with
   `{"hookSpecificOutput": {"permissionDecision": "allow" | "deny"}}`, which
   Claude Code honors in place of the terminal approval dialog. Canonical
   source: <https://code.claude.com/docs/en/hooks.md>. So an iOS button tap
   can actually resolve the pending tool call via local IPC that unblocks the
   hook — this isn't a fire-and-forget ping.

## 3. Components

### 3a. `IOSSink` (Mac-side, new sink)

Mirrors the existing `SlackSink` / `DiscordSink` shape — a dataclass wrapping
its config, a `send(event)` method, wired into `cli._dispatch`.

- HTTP/2 POST to `api.push.apple.com` (prod) / `api.sandbox.push.apple.com`
  (dev). Auth via JWT signed with the p8 APNs auth key (ES256).
- JWT cached 20–55 min per APNs policy; refreshed lazily.
- Key storage: macOS Keychain via the `security` CLI or PyObjC Security
  framework — never on disk in plaintext. Read at startup, held in memory.
- Two push types:
  - `alert` (standard) for permission / idle_prompt / turn_complete /
    elicitation.
  - `liveactivity` for Live Activity content-state updates. Requires the
    separate topic suffix `.push-type.liveactivity`.
- `apns-priority: 10` for user-visible alerts; `5` for Live Activity routine
  updates to conserve battery.
- New sink lives at `src/coding_agent_notifier/sinks/ios.py`. Shares
  `http_post_json` where possible but APNs requires HTTP/2 — this sink is the
  only caller that needs it, so either (a) `hyper`/`httpx` gated to this sink,
  or (b) use `system(curl --http2 ...)` to keep pure-stdlib runtime for the
  non-iOS path. The implementation pass picks one; the doc calls out the
  tradeoff.

### 3b. Notification Service Extension (iOS)

Runs in its own process; intercepts every push where `mutable-content: 1`.

- Reads `payload.encrypted` (base64 AEAD ciphertext).
- Fetches the master key from iOS Keychain (Secure-Enclave-wrapped attribute
  where hardware supports it).
- Decrypts (XChaCha20-Poly1305, see §4) → plaintext JSON with event fields.
- Rewrites `bestAttemptContent.title`, `.body`, `.categoryIdentifier`
  (`approve_deny` for permission pushes, nothing for passive pings),
  `.userInfo["event_id"]`, `.userInfo["approval_token"]`.
- Calls `contentHandler(bestAttemptContent)`.
- On decrypt failure: falls back to the APNs-delivered envelope (which
  intentionally contains no sensitive data — see §4). Never displays
  ciphertext.

Runtime budget: 30 s wall, ~24 MB RAM. Plenty of headroom for AEAD decrypt
and an optional Tailscale fetch.

### 3c. iOS companion app

SwiftUI app whose primary job is to be a *notification registration target*
and *action-handling delegate*. The app itself is rarely opened.

- Registers for remote notifications on launch; sends the APNs device token
  to the Mac over Tailscale during pairing.
- Declares `UNNotificationCategory(identifier: "approve_deny")` with two
  `UNNotificationAction`s — `approve` and `deny` — both with
  `.authenticationRequired` so the lock-screen prompt forces Face ID / Touch
  ID / passcode before the action fires.
- `UNUserNotificationCenterDelegate.didReceive(response:)` handles the action
  in the background. Opens a signed POST (§4) to the Mac's Tailscale endpoint
  and completes within the ~30 s background budget.
- Starts/ends `ActivityKit` Live Activities in response to lifecycle events.
  Live Activity push token (distinct from device token) is sent to the Mac
  each time an activity starts, stored against the `session_id`, and used as
  the `apns-topic` for subsequent update pushes.
- Hosts the pairing UI: QR scan of a short-lived token printed by
  `agent-notify pair --ios` on the Mac, followed by X25519 ECDH handshake
  (§4).
- Also hosts a plaintext scroll-back of recent events, if we decide to keep
  one on-device — see open question in §11.

### 3d. `PendingApprovals` registry (Mac-side, new module)

Lives at `src/coding_agent_notifier/pending_approvals.py`. Mirrors the shape
of the existing `src/coding_agent_notifier/pending.py` — fcntl-locked JSON,
same `XDG_CACHE_HOME` root, separate subdir `pending_approvals/`.

- Keyed by a per-approval UUIDv4 generated at hook entry.
- `PreToolUse` hook writes a request entry and then `os.open(...O_RDONLY)` on
  a named FIFO at `pending_approvals/<uuid>.fifo` (or `select()`s on a UNIX
  domain socket — whichever is easier to test). The blocking read is the
  "wait for iOS" mechanism.
- The Mac-side HTTP listener (§3e) writes the decision into the registry and
  unblocks the hook by writing one byte to the FIFO.
- Hook reads the decision, prints Claude Code's expected JSON shape on
  stdout, exits. If the hook times out before the FIFO fires → default to
  `deny` (fail-closed). This is a deliberate choice: a notifier that silently
  approves on timeout is much worse than one that silently denies.
- GC of stale entries is reused from the existing `pending.py` pattern — same
  `max_age_seconds` cleanup on writer-side.

### 3e. Local HTTPS listener on Mac (Tailscale-bound)

A small HTTP server (stdlib `http.server` or `aiohttp` — decide during impl)
that the iOS app hits for three routes:

- `POST /v1/approve` — signed approve/deny response (§4).
- `POST /v1/register` — called during pairing: APNs device token + ECDH
  public key exchange.
- `POST /v1/fetch/<token>` — one-time fetch of a large encrypted event blob
  when the APNs push used the fetch pattern (§4).

- **Binds only to the Tailscale interface IP** (`100.x.y.z`), never
  `0.0.0.0`. We refuse to bind if Tailscale is not up. This is the single
  most important security property of the whole system; it must be an
  assertion, not a hope.
- Tailscale ACLs further restrict which tailnet nodes may reach the port —
  by default, only the paired iPhone.
- Optional hardening: mTLS using the Tailscale-issued node cert as an extra
  belt. Not required for v1 since Tailscale already provides WireGuard
  transport auth, but note as a future option.

## 4. Crypto design

All symbols concrete enough that a reviewer can catch a mistake.

### Pairing

1. Mac: `agent-notify pair --ios` prints a QR code encoding
   `{"hostname": "mac.tail-xxxx.ts.net", "port": 8443, "pair_token": <24-byte-urlsafe>}`
   and its ephemeral X25519 public key.
2. iPhone: scans, generates its own X25519 key in Secure Enclave, POSTs its
   public key + the `pair_token` to `https://<hostname>:<port>/v1/register`.
3. Both sides compute shared secret via X25519.
4. Derive 32-byte master key `K_master = HKDF-SHA256(shared, salt=pair_token, info="agent-notify/v1/master")`.
5. Derive subkeys:
   - `K_push = HKDF(K_master, info="push")` — push AEAD.
   - `K_fetch = HKDF(K_master, info="fetch")` — fetch-URL AEAD.
   - `K_sig = HKDF(K_master, info="sig")` — input key material for the
     Ed25519 signing pair. Ed25519 is deterministic; both sides compute the
     same signing keypair from `K_sig`. (Alternative: generate Ed25519 at
     pairing and exchange public-half only. Either is acceptable; the
     implementation pass picks one.)
6. Mac stores `K_master` in macOS Keychain (ACL: only `agent-notify` tool).
   iPhone stores in iOS Keychain with `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`
   + Secure Enclave wrapping where hardware allows.

### Push payload AEAD

- Algorithm: **XChaCha20-Poly1305** (`pynacl` on Mac, CryptoKit's
  `ChaChaPoly` on iOS — but note CryptoKit's built-in is ChaCha20-Poly1305
  with 12-byte nonce; for XChaCha20's 24-byte nonce either bring libsodium
  via SwiftSodium, or switch both sides to ChaCha20-Poly1305 with a
  message-counter nonce. Decide in impl; call out as crypto-review item).
- Nonce: 24 random bytes per message (XChaCha20) or 12-byte counter
  (ChaChaPoly). MUST never repeat under a given key; counter approach uses a
  per-session counter persisted on Mac and rejected below high-water on
  iOS.
- AAD binds context: `aad = pack(version_u8, kind_u8, timestamp_u64_be, counter_u64_be)`.
  This means replay of a captured ciphertext under a different `kind` or
  window fails.
- APNs payload shape:

```json
{
  "aps": {
    "alert": { "title": "Agent", "body": "activity" },
    "mutable-content": 1,
    "category": "approve_deny",
    "sound": "default"
  },
  "v": 1,
  "kind": "permission",
  "enc": "<base64(nonce || ciphertext || tag)>",
  "ts": 1735689600,
  "ctr": 42
}
```

The top-level `aps.alert` is intentionally generic — it's what the user sees
if the NSE fails to decrypt. Nothing sensitive leaks. All meaningful content
(tool name, tool input, snippet, approval token) lives inside `enc`.

### Size budget and the fetch pattern

APNs caps payloads at 4 KB (standard) / 4 KB (Live Activity update). Small
events fit inline. Large events (turn_complete with a long transcript
snippet) won't — they use the fetch pattern:

1. Mac generates a random 32-byte `fetch_token`, stores ciphertext under
   `/tmp/agent-notify-fetch/<fetch_token>` (fcntl-locked, 0o600, expires on
   first read or 60 s).
2. APNs payload omits `enc`, includes `fetch_url` = `https://<tailnet>:8443/v1/fetch/<fetch_token>`.
3. NSE fetches, decrypts with `K_fetch`, mutates content as usual.
4. Tailscale encrypts the fetch transport; AEAD encrypts the payload; the
   one-time token binds which iPhone may retrieve which blob.

### Replay protection

- AAD includes timestamp (±2 min skew window) and monotonic counter.
- iOS NSE keeps per-key high-water counter in Keychain; rejects any incoming
  message with `counter <= high_water`. Updates on each successful
  decryption.
- On Mac, counter persists next to `K_master` and is atomically incremented
  with the same fcntl lock that guards the key.

### Approve/deny response signing

iOS signs the response POST body with the Ed25519 keypair derived at
pairing:

```json
{
  "v": 1,
  "event_id": "<uuid>",
  "decision": "allow",
  "ts": 1735689600,
  "sig": "<base64(Ed25519(K_sig_priv, canonical_json({v, event_id, decision, ts})))>"
}
```

Mac verifies signature before writing to `PendingApprovals`. Rejects unsigned
responses unconditionally. Prevents a compromised tailnet peer (say, a
neighboring laptop sharing the tailnet) from minting approvals.

### Key rotation

v1: manual re-pair. `agent-notify pair --rotate` on Mac invalidates the old
`K_master`, prints a new QR, iOS re-pairs. Old PendingApprovals entries are
dropped because their `approval_token`s won't verify under the new `K_sig`.

Automatic time-based rotation of the *derived* subkeys (rotating
`K_push`/`K_fetch` every N days with `info` string including an epoch index)
is out of scope for v1; noted as straightforward follow-up.

## 5. Threat model

| # | Asset / threat                         | Mitigation                                                                                   | Residual risk                                                                                    |
| - | -------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 1 | Apple APNs reads payload               | Full content in AEAD `enc`; APNs-visible envelope is generic                                 | Event `kind` (8 values) and `ts` leak; acceptable                                                |
| 2 | Mac compromise (malware as user)       | Keychain ACLs; no plaintext keys on disk; don't log `tool_input`                             | Full TCB loss — attacker is the sender. Nothing in scope fixes this; limit via `tool_input` redaction in any logs |
| 3 | iPhone theft                           | Secure Enclave + passcode/biometric; Keychain `ThisDeviceOnly`; `agent-notify pair --revoke` | Offline brute-force of short passcodes is an Apple-platform concern, not ours                     |
| 4 | Tailnet peer compromise                | mTLS (optional) + signed approve/deny + AEAD push + fail-closed approval timeout             | Compromised peer can DoS (see row 8); cannot forge approvals                                     |
| 5 | Replay of captured APNs push           | AAD timestamp (±2 min) + monotonic counter rejected on iOS side                               | Real-time MITM within window is possible but doesn't decrypt anything new                        |
| 6 | Spoofed approve/deny                   | Ed25519 signature required; `PendingApprovals` rejects unsigned                              | If `K_sig` is exfiltrated from iOS Keychain, attacker can forge — caught by row 3 mitigations    |
| 7 | NSE crash / >30 s timeout              | APNs-visible envelope is non-sensitive; NSE never shows ciphertext                            | User sees a generic "Agent activity" notification until they open the app                       |
| 8 | DoS against Mac HTTP listener          | Tailscale ACL limits peers; per-peer token-bucket rate limit; bind Tailscale-only            | A compromised paired iPhone can flood; mitigation is revoke + re-pair                            |
| 9 | Fetch-token theft in transit           | One-time use, 60 s TTL, tailnet-only endpoint, Ed25519-bound requestor check                  | A tailnet-local MITM in the brief window could race the legit NSE; refuses the slower one        |

Review notes:

- Row 2 is the fundamental limit. If the Mac is compromised, nothing we do at
  the notifier layer helps. Write it down so no one spends time "hardening"
  against the in-TCB attacker.
- Row 4 implies we should not *default to* enabling the HTTP listener on a
  multi-user tailnet. The doc's default tailnet ACL template pins the iPhone
  by node ID.
- Row 9 means the NSE must handle "fetch returns 404" gracefully —
  `contentHandler(bestAttemptContent)` with the APNs-visible envelope, not a
  retry loop.

## 6. Hook wiring (Mac side)

Registered only if `[ios] actionable_approvals = true`:

- `PreToolUse` — blocking. Writes a `PendingApprovals` entry, sends APNs push
  via `IOSSink`, `read()`s on the FIFO until iOS answers or hook `timeout`
  hits. Emits `{"hookSpecificOutput": {"permissionDecision": "<allow|deny>"}}`.

Why `PreToolUse` and not `PermissionRequest`:

- `PermissionRequest` is notification-only; it can't return a decision that
  Claude Code honors.
- `PreToolUse` supports `permissionDecision` on stdout and is the documented
  surface for programmatic approval. (Verified via `claude-code-guide`; canonical
  at <https://code.claude.com/docs/en/hooks.md>.)

Coexistence with the existing sinks:

- Slack/Discord continue to receive the same event but as a *notification*
  (no action buttons). They are the visibility fallback: if iOS is off the
  tailnet, the user can still see the agent is blocked.
- The Slack notification text should include the `session_id` so the user
  can recover via the terminal if iOS approve/deny never arrives. No
  functional regression vs. today.

`timeout` configuration: default 600 s to match Claude Code's default; raise
to 3600 s via the hook's `timeout` key once we're confident in the round-trip
path. Default stays conservative.

## 7. Config surface

New TOML sections, parsed in `src/coding_agent_notifier/config.py` with the
existing `ConfigError`-on-misconfig pattern:

```toml
[ios]
enabled = true
actionable_approvals = true        # wire the blocking PreToolUse hook
apns_key_id = "ABCD1234"
apns_team_id = "TEAM5678"
apns_bundle_id = "com.example.agent-notify"
listener_address = "100.64.0.2:8443"   # Tailscale-only bind
listener_tls = "auto"                  # or path to cert/key

[ios.live_activity]
enabled = true
end_after_seconds = 28800              # 8h Apple default
```

Parser invariants:

- `enabled = true` with any missing required field → `ConfigError`.
- `listener_address` must resolve to a tailnet IP (prefix `100.`) — otherwise
  refuse to start. A safety rail against accidentally binding to a LAN
  interface.
- `actionable_approvals = true` without `enabled = true` → `ConfigError`.

## 8. iOS project layout (sketch)

- SwiftUI app, min iOS 17 (Live Activities push-update GA'd 16.2; widget
  features and depth we want shipped in 17; no good reason to support older).
- Three targets in one Xcode project:
  - `AgentNotifyApp` — registration, pairing UI, action delegate, Tailscale
    POST.
  - `AgentNotifyNSE` — Notification Service Extension; decrypt + mutate.
  - `AgentNotifyWidgets` — ActivityKit widget bundle (Lock Screen + Dynamic
    Island).
- App Group identifier shared between all three for Keychain access to
  `K_master`.
- Separate repo: `coding-agent-notifier-ios`. Keeps the Python repo
  dependency-light (no `.xcodeproj` clutter), lets iOS ship under its own
  signing identity + release cadence. Main repo links it from the README.

## 9. Prereqs & costs

- Apple Developer Program membership ($99/yr). Free-tier provisioning rotates
  every 7 days — not viable for a daily-use notifier.
- Xcode 16+, physical iPhone for APNs testing (simulator won't register for
  remote pushes).
- Tailscale installed on both Mac and iPhone; tailnet ACL allowing the
  iPhone → Mac's `agent-notify` listener port.
- New Python dependency: `pynacl` (libsodium bindings) — accepted cost for
  the iOS feature, gated to the iOS sink code path. Slack/Discord sinks
  stay dep-free (only `tomlkit`, per existing CLAUDE.md policy).

## 10. Prototyping order

Designed so each step delivers something testable and the later, more
ambitious steps can be deferred without invalidating earlier work:

1. **`IOSSink` + throwaway iOS app, unencrypted.** Prove APNs plumbing end
   to end — p8 auth, JWT, topic, delivery. No NSE, no E2EE yet. This is
   mostly Python.
2. **NSE + AEAD decrypt.** Ship the crypto (§4). Delete the unencrypted
   path; it never ships.
3. **Live Activities (read-only).** ActivityKit token handoff + start/end/
   update. No approve/deny yet.
4. **`PendingApprovals` + signed approve/deny roundtrip.** Mac HTTP
   listener, iOS action delegate, Ed25519 verification. The big lift —
   this is where the system becomes genuinely useful, not just pretty.
5. **Blocking `PreToolUse` hook wired to `PendingApprovals`.** Turn the
   pretty demo into a real gate.
6. **Pairing UX.** QR scan, revoke, key rotation doc.
7. **Threat-model re-review.** Fresh eyes on §5 before any `v1` tag. A
   pair of outside eyes at this stage is worth the delay.

Even if step 4 or 5 proves infeasible on some iOS-API limit we haven't
anticipated, steps 1–3 ship a strictly better iOS notifier than Slack. No
step is wasted.

## 11. Open questions

- **Per-kind subkeys?** §4 uses one `K_push` across all event kinds. An
  alternative is one subkey per kind, so a cryptanalytic weakness on one
  kind doesn't bleed into others. Marginal benefit vs. more moving parts.
  Punt to impl, default single-key.
- **iOS-side event history?** Either the iPhone is a pure viewer and the Mac
  is sole source of truth, or the iPhone keeps a SwiftData store of recent
  events for offline review. Second is nicer UX but adds a sync layer and
  another place for secrets to live. Lean toward first for v1.
- **PWA + Web Push as an alternative?** Rejected. iOS Safari's Web Push
  shipped in 16.4 but doesn't support Live Activities, doesn't support
  actionable notification categories with background-handled actions, and
  can't use a Notification Service Extension. It's sufficient for a
  one-way ping, but that's already what Slack gives us. The whole point of
  this proposal is what Web Push *can't* do.
- **Audit log of approvals on the Mac?** Recommendation: yes, append-only
  JSONL next to `defer.log`, `tool_input` redacted per existing CLAUDE.md
  policy of not logging secrets. Exact format TBD in impl.

## Out of scope for this doc

- Swift or Python code.
- APNs connection prototyping.
- iOS app bundle ID / App Store strategy (and whether it's even needed —
  personal signing may be enough for a single-user tool).
- Multi-device pairing (>1 iPhone per Mac).
- Android.
