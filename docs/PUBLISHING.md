# Publishing this repository to GitHub

Step-by-step instructions to create `Erwin60/sota_prominence` and push the
contents. Two paths are given: the **GitHub CLI** (`gh`, fastest) and the
**web + git** path. Pick one.

The repository does **not** yet exist on GitHub — creating it is part of these
steps.

---

## Path A — GitHub CLI (`gh`)

Assumes `gh` is installed and you are logged in (`gh auth login`).

```bash
# 1. Go to the prepared repository folder (the one containing README.md, scripts/, docs/)
cd /path/to/sota_prominence

# 2. Initialise git and make the first commit
git init
git add .
git commit -m "Initial release: AT-SOTA prominence pipeline (Steps 1-5d)"

# 3. Create the GitHub repo and push in one step
#    --public (or --private for a staged release before publication)
gh repo create Erwin60/sota_prominence \
  --public \
  --source=. \
  --remote=origin \
  --description "Divide-consistent QGIS/PixelMinimax workflow for SOTA 150 m prominence in Austria" \
  --push

# 4. Verify
gh repo view Erwin60/sota_prominence --web
```

That's it — the repo is created and the initial commit is pushed to `main`.

---

## Path B — Web UI + git

```bash
# 1. Create the empty repo on github.com:
#    New repository -> Owner: Erwin60-> Name: sota_prominence
#    Do NOT add a README, .gitignore, or license (this repo already has them).

# 2. In the prepared folder:
cd /path/to/sota_prominence
git init
git add .
git commit -m "Initial release: AT-SOTA prominence pipeline (Steps 1-5d)"
git branch -M main
git remote add origin https://github.com/Erwin60/sota_prominence.git
git push -u origin main
```

---

## After the first push

* Confirm `README.md` renders on the repo landing page.
* Check that `intermediate/`, `results/`, `raw/`, and `*.gpkg`/`*.tif` are
  ignored (they are listed in `.gitignore`) — no large geodata should be
  committed.
* Optionally create a release tag once the paper is accepted:

  ```bash
  git tag -a v5.2 -m "Release accompanying the SOTA prominence paper"
  git push origin v5.2
  ```

* For an archival DOI (recommended for a paper), connect the repository to
  **Zenodo** and cut a GitHub release; Zenodo mints a DOI automatically. Add
  that DOI to the paper's *Supplementary Material* section, which currently
  states the archival snapshot will follow on publication.

---

## Note on the paper and presentation

* The **presentation** files are intentionally **not** part of this repository.
* The **paper** (`Prominence_v5_2.tex/.pdf`) is not required to live in the
  repository. Including a preprint PDF is optional and common, but check the
  target journal's policy on posting the accepted manuscript. The paper's
  *Code Availability* section already points here
  (`https://github.com/Erwin60/sota_prominence`).
