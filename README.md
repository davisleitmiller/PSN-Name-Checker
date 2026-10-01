# PSN Online ID Availability Checker

A small Python script that checks whether PlayStation Network **online IDs**
(usernames) are available, using PSN's own availability endpoint. It can
check a single name, a list from a file, or generate large batches of names.

> **Disclaimer:** This is an unofficial tool and is not affiliated with Sony
> or PlayStation. Bulk automated access may violate PSN's terms of service.
> Use it responsibly, at a conservative rate, and do not use it to hoard or
> squat usernames. Provided for educational purposes only.

---

## How it works

It sends the same request the PlayStation website uses when it checks a name:

```
POST https://accounts.api.playstation.com/api/v1/accounts/onlineIds
Content-Type: application/json; charset=UTF-8
Body: {"onlineId": "<name>", "reserveIfAvailable": false}
```

No login or NPSSO token is required. `reserveIfAvailable` is `false`, so the
check **never reserves or claims** a name — it only reports availability.

### Response codes

| HTTP | `X-ErrorCode` | Meaning |
|------|---------------|---------|
| `200`–`299` | – | **Available** |
| `400` | `accounts:3101` | Taken |
| `400` | `accounts:3208` | Improper (policy) |
| `400` | `korra:1100` | Invalid pattern / length |
| `406` | – | Rejected by policy |
| `429` | – | Rate limited (backs off) |

> **Minimum length:** PSN rejects every online ID **shorter than 5
> characters** with `406`. 3- and 4-character IDs are no longer registrable,
> so the script refuses `--length < 5` unless you pass `--allow-short`.

---

## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.8+.

---

## Usage

Check a specific list of names (see [`examples/names.txt`](examples/names.txt)):

```bash
python psn_id_checker.py --from-file examples/names.txt
```

Generate and check every 5-letter name starting with `d` and ending with `s`:

```bash
python psn_id_checker.py --length 5 --starts-with d --ends-with s
```

Use a positional pattern (`C` = consonant, `V` = vowel, `?` = any, other
characters are literals):

```bash
# name-like 5-letter IDs shaped like "davis" (d + V + C + V + s)
python psn_id_checker.py --length 5 --pattern dVCVs

# restrict the alphabet (e.g. drop awkward letters)
python psn_id_checker.py --length 5 --charset acemnorsuvw --pattern CVCVC
```

Preview without sending any requests:

```bash
python psn_id_checker.py --length 5 --pattern CVCVC --dry-run 20
```

Available names are appended to `available_ids.txt`; every definite result is
written to `checked_ids.txt` so an interrupted run resumes where it left off.

Run `python psn_id_checker.py --help` for all options.

---

## Notes & tips

* **Resume:** re-run the same command and already-checked names are skipped.
* **Pacing:** requests are throttled with an adaptive delay that backs off on
  `429`/`5xx`. Keep `--workers` low (1–2); higher values may get the IP
  temporarily blocked (Akamai returns `403`).
* **Reality check:** short, name-like IDs are almost always taken. If you are
  hunting for a pretty name, longer IDs (6+ characters) have far more
  availability.
* A `201`/available result is a strong signal, but confirm the name in the
  actual rename flow before relying on it.

---

## License

MIT — see [LICENSE](LICENSE).
