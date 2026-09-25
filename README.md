# AltStore PAL source

A self-hosted [AltStore PAL](https://altstore.io) source served from GitHub Pages.

- **GitHub Pages** serves `index.html` (the landing page), `source.json` and `assets/` (icons and screenshots).
- **GitHub Releases** hold each notarized ADP (`manifest.json`, `signature` and the `.ipa` variants), byte for byte as Apple produced it. `source.json` points to them through `downloadURL` and `assetURLs`.
- The **Publish ADP** workflow does a release end to end: ADP ID in, updated source out.

Source URL once deployed: `https://brighthills.github.io/altstore/source.json`

## 1. One-time Apple / AltStore setup

1. **EU terms.** The Account Holder accepts the updated Apple Developer Program License Agreement in the developer account. Its Attachment 14 holds the EU terms for alternative distribution, in effect from October 1, 2026. It replaces the old Alternative Terms Addendum. Free apps pay no commission. Sales of digital goods and services pay a 5% Core Technology Commission. See [Changes for apps in the EU](https://developer.apple.com/support/apps-in-the-eu/).
2. **Developer ID.** In App Store Connect → your name → *Edit Profile*, copy the **Developer ID** UUID. It is not the Team ID.
3. **Register with AltStore PAL:**
   ```bash
   curl -H "Content-Type: application/json" -X POST \
     --data '{"developerID":"<Developer ID UUID>","email":"<your email>"}' \
     https://api.altstore.io/register
   ```
   Keep the `token` from the response. It has an `expiration`.
4. **Authorize AltStore as a marketplace.** App Store Connect → *Users and Access* → *Integrations* → *Marketplace* → `+`. Paste the token, select the apps and choose automatic processing.
5. **Notarize.** For each app: *App Review* → set *Review Type* to **Notarization**, then submit. Apps already on the App Store are notarized automatically.
6. **Apple ID.** App Store Connect → app → *App Information* → **Apple ID**. This goes into `marketplaceID`.

## 2. One-time repo setup

1. Create the **public** repo `brighthills/altstore` (free Pages needs a public repo) and push this folder to `main`.
2. `source.json` already points at `https://brighthills.github.io/altstore`. If the org or repo name changes, update the URLs in `source.json` to match.
3. Fill in every `REPLACE_ME` in `source.json`: app name, `bundleIdentifier` (case-sensitive, must match the build), `marketplaceID`, descriptions and `appPermissions`. The format is described in the [source docs](https://faq.altstore.io/developers/make-a-source).
4. Add the images referenced in `source.json`:
   - `assets/source-icon.png`
   - `assets/source-header.png`
   - `assets/app/icon.png`
   - optional screenshots under `assets/app/`
5. Run `python scripts/publish_adp.py validate` until it prints `source.json OK`.
6. Repo *Settings* → *Pages* → *Source*: **GitHub Actions**.
7. Push. The **Deploy Pages** workflow validates `source.json` and publishes the site. It refuses to deploy while placeholders or missing images remain.

To add more apps later, add another entry to `apps` in `source.json` (`versions: []`) before publishing its first ADP.

## 3. Publishing a release

1. Wait until the version is notarized. Then copy its **Alternative Distribution Package ID** from App Store Connect → app → *Distribution* → *History*.
2. Open *Actions* → **Publish ADP** → *Run workflow*. Paste the ADP ID and optional release notes.
3. The workflow then:
   1. asks `api.altstore.io` to process the ADP and waits for its download URL;
   2. unzips it and reads bundle ID, version, build and minimum iOS from the IPAs' `Info.plist`;
   3. creates the release `<bundleId>-<version>-<build>` with every ADP file attached, unmodified;
   4. prepends the version to the matching app in `source.json` (`downloadURL` = the release's `manifest.json`, `assetURLs` = every file) and commits it;
   5. redeploys Pages.

AltStore PAL checks sources periodically, so users get the update automatically.

If `main` is branch-protected, allow `github-actions[bot]` to push, or the commit step fails.

### Running it locally

Needs Python 3.9+ and an authenticated [`gh`](https://cli.github.com).

```bash
python scripts/publish_adp.py publish --adp-id <ADP ID> --notes "Bug fixes"
python scripts/publish_adp.py publish --adp-zip path/to/adp.zip --dry-run   # no upload
git add source.json && git commit -m "Publish …" && git push
```

## 4. Sharing the source

- The landing page has an **Add to AltStore PAL** button (`altstore-pal://source?url=…`) and a copy-URL button.
- Optional: to be listed on [explore.alt.store](https://explore.alt.store), set `fediUsername` in `source.json` and federate:
  ```bash
  curl -H "Content-Type: application/json" -X POST \
    --data '{"source":"https://brighthills.github.io/altstore/source.json"}' \
    https://api.altstore.io/federate
  ```

## References

- [Distribute with AltStore PAL](https://faq.altstore.io/developers/distribute-with-altstore-pal)
- [Make a Source](https://faq.altstore.io/developers/make-a-source)
- [AltStore PAL REST API](https://faq.altstore.io/developers/rest-api)
- [Get an Alternative Distribution Package ID (Apple)](https://developer.apple.com/help/app-store-connect/distributing-apps-in-the-european-union/get-an-alternative-distribution-package-id/)
