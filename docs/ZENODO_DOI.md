# Obtaining a Zenodo DOI for this repository

This guide walks through archiving the repository on **Zenodo** and minting a
citable **DOI**. Zenodo integrates directly with GitHub: you connect the
repository once, publish a release, and Zenodo archives it and assigns a DOI
automatically.

Do this once the code is ready to be cited in the paper (typically on paper
acceptance).

---

## Prerequisite — the repository must be public

Zenodo can only archive **public** GitHub repositories; it cannot see a private
one. If `Erwin60/sota_prominence` is still private, make it public first:

```bash
gh repo edit Erwin60/sota_prominence --visibility public
```

Confirm the change:

```bash
gh repo view Erwin60/sota_prominence   # "Visibility: public"
```

You can prepare Steps 1–2 while private, but the actual archiving (Steps 3–4)
only works once the repository is public.

---

## Step 1 — Create a Zenodo account and connect GitHub

1. Go to <https://zenodo.org>.
2. Sign in with **"Sign in with GitHub"** — this links the two accounts in one
   step. (Zenodo is operated by CERN and is the standard for software DOIs.)

---

## Step 2 — Enable the repository for Zenodo

1. In Zenodo, top right → your name → **GitHub**.
2. Find `Erwin60/sota_prominence` in the repository list and flip the switch to
   **On**.
3. If it does not appear, click **Sync now** (newly created or newly-public
   repos can take a moment). On first enable, approve the Zenodo webhook when
   GitHub asks for permission.

> **Important:** the switch must be **On before** you publish the release.
> Zenodo only archives releases created *after* enabling; earlier releases are
> not captured retroactively.

---

## Step 3 — Publish a GitHub release

The Zenodo webhook fires on a **release**, not on a bare tag push.

Using the GitHub CLI:

```bash
cd ~/Documents/Amateurfunk/_SOTA/Prominence/sota_prominence
gh release create v5.2 \
  --title "v5.2 — SOTA prominence pipeline" \
  --notes "Release accompanying the SOTA prominence paper (Steps 1-5d)."
```

Or in the browser: repo → **Releases** → **Draft a new release** → tag `v5.2` →
**Publish release**.

Once published, Zenodo automatically fetches the release ZIP, archives it, and
mints a DOI — usually within a minute or two.

---

## Step 4 — Retrieve the DOI

Back in Zenodo under **GitHub**, a DOI badge now appears next to the repository.
Zenodo issues **two** DOIs:

* a **version DOI** — points to exactly v5.2;
* a **concept DOI** — always points to the latest version.

For the paper, use the **concept DOI**: it stays valid even if you later publish
v5.3.

---

## Step 5 — Check the metadata

Open the Zenodo record and verify the author (Erwin Grabler), title, license
(MIT), and description. Zenodo pulls much of this from the repository and from
`CITATION.cff`; you can edit any field directly in the record. Add your ORCID in
`CITATION.cff` (and/or the record) if you have one, so future releases carry it
automatically.

---

## Step 6 — Add the DOI to the paper

In the paper's *Supplementary Material* section, insert the archival DOI, for
example:

```latex
\section*{Supplementary Material}
The intermediate layers and visualisation styles are maintained as
project materials accompanying this workflow. An archival snapshot of the
source code is deposited on Zenodo:
\url{https://doi.org/10.5281/zenodo.XXXXXXX}.
```

Replace `XXXXXXX` with your **concept DOI** number.

---

## Later versions

For each new version, repeat only Step 3 (publish a new release). Zenodo
archives it automatically: the version DOI is new, the concept DOI stays the
same, so the citation in the paper remains valid.
