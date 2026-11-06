# Limulator website

This is a dependency-free static site. From this directory:

```sh
npm run build
npm run preview
```

Preview runs at `http://127.0.0.1:4173`. Set `PORT` to choose another local port. `dist/` is generated from `src/` and is ignored by Git.

The built assets and evidence paths are relative, so the site can also be served from a repository subdirectory such as `/limulator/`.

## Updating project evidence

The site reads its validation state from `src/data/evidence.json` and device profiles from `src/data/profiles.json`. Keep those files current as native checks complete.

Add only real guest PNGs under `src/assets/native/`, then add an entry to `captures` with its `file`, `title`, `alt`, `context`, `device`, `os`, and `posture`. The gallery intentionally stays empty until that evidence exists. App performance results belong in `applicationMeasurements`; keep workload, method, and environment context with each measurement.

Profiles describe validation state, not general iOS support. Update the model, OS, scale, geometry, and evidence status together when a profile is tested.
