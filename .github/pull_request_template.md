> **Write for the reviewer.** The description should read in a minute: what
> changes, why, and what to look at closely. No tables, no decorative sections,
> nothing the diff already shows. If it does not fit in a few lines, the PR is
> too big: split it. Technical detail goes in the issue or the commit message.
>
> **A diagram when it explains better than text.** For flows, execution order or
> how components relate, a diagram replaces paragraphs: use it instead of the
> prose, not as well as it.

## 📝 Description
<!-- One line in the commit template's format -->
<!-- Example: :bug: fix(audition): never take the studio take for a live single -->

**Detailed context:**
<!-- What changes and why, in a few lines. Link issues (Closes #123) -->

---

## 🧪 Technical verification
- [ ] **Local tests**: `.venv/bin/python -m pytest -q tests` passed.
- [ ] **Live stack, read-only**: any tool touched was run against the real stack without changing it (`--dry-run`, `--status`, or the free pass without `--download`).
- [ ] **Bridge image**: if `bridge.py`, `Dockerfile` or `deploy/` changed, the image was rebuilt and the bridge answered.
- [ ] **systemd units**: if `systemd/` changed, the copies in `~/.config/systemd/user/` were updated and `systemctl --user daemon-reload` was run.
- [ ] **Secrets**: no API key, password or `.env` content in the diff.

---

## 📸 Attachments (optional)
<!-- Output of a real run, if it shows the change -->
