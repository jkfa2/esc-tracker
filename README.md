# Site monitor

Watches websites and pings you on Discord when something changes. Runs for free on GitHub Actions, no computer of your own needed. Five kinds of watcher:

| type | what it does |
|---|---|
| `sitemap` | Tracks `lastmod` in sitemaps. Follows sitemap indexes into the child sitemaps and tells you **which URLs** are new or changed. Optionally shows **what changed on those pages**. |
| `wordpress` | Polls a WP REST endpoint (`/wp-json/wp/v2/posts`, `/media`, custom post types…). Reports new IDs and changed `modified` dates. Optionally shows **what changed in the text**. |
| `rss` | Reports new feed items (title, summary, link), oldest first. |
| `page` | Watches a whole page or one part of it (CSS selector). |
| `pdf` | Watches a PDF file and shows **which sentences changed**, even if the layout was re-flowed. |

Everything is configured in **`config.yaml`**. The first run of a new watcher only records a baseline, so you won't get flooded.

Ready-to-paste examples of every option are in **`config-examples.yaml`**.

---

## Setup (about 10 minutes)

**1. Make a Discord webhook.** In your Discord server: channel ⚙️ → Integrations → Webhooks → New Webhook → *Copy Webhook URL*.

**2. Create a GitHub repository** and upload everything in this folder. Keep the `.github/workflows/monitor.yml` path intact. The `.github` folder is hidden on a Mac: press **Cmd + Shift + .** in Finder to show it, or create the file in GitHub with *Add file → Create new file* and type `.github/workflows/monitor.yml` as the name.
- **Public repo:** Actions are free and unlimited. Your watch list and the saved snapshots in `state/` are then visible to anyone. The webhook URL stays secret.
- **Private repo:** free, but capped at 2,000 minutes/month on the Free plan. The default 15-minute schedule would exceed that. In `monitor.yml`, change `*/15` to `*/30` or slower.

**3. Add the webhook as a secret.** Repo → Settings → Secrets and variables → Actions → *New repository secret*. Name: `DISCORD_WEBHOOK_URL`, value: the URL from step 1.

**4. Run it.** Actions tab → "Site monitor" → *Run workflow*. After that it runs by itself every ~15 minutes (GitHub may delay runs by a few minutes).

---

## Adding a new website

You never take a snapshot by hand. The first run of any new watcher records the current state as the starting point, silently. From then on, changes are reported.

1. **Pick the best source.** In this order: an RSS feed (try `/feed`, `/rss`, or look for the RSS icon), then a WordPress API (add `/wp-json/wp/v2/posts` to the domain and see if it loads), then a sitemap (`/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`), and a normal `page` watcher as the last resort.
2. **Copy a block** of the same type in `config.yaml` (or from `config-examples.yaml`), and change `name`, `url` and any options.
3. **Commit the change.** Saving `config.yaml` starts a run automatically. The new watcher records its baseline.
4. **Check the run once.** Actions tab → open the latest run → the log should say `baseline recorded` for the new watcher. A line starting with `✗` means something is wrong, such as a 403 or a wrong URL.

To start over for one watcher, delete its file in `state/` (for example `state/malta-media.json`). Its next run is a silent baseline again.

Optional, on your own computer: `python monitor.py test "Watcher name"` shows what a watcher currently sees (and which items your keywords would catch) before you commit it.

---

## Pausing and resuming

Edit `config.yaml` on GitHub (the mobile app works too), change `enabled: true` ↔ `enabled: false` for a watcher, and commit. To pause everything: Actions tab → Site monitor → **⋯** → *Disable workflow*.

A paused watcher keeps its state, so when you resume, you hear about everything that changed in between. Delete its file in `state/` to skip that.

---

## Page watchers: `mode: text` or `mode: links`?

Ask yourself: **is this page a list that grows, or a page that someone edits?**

| | `mode: text` (default) | `mode: links` |
|---|---|---|
| Question it answers | "Did the words on this page change?" | "Did new links appear on this page?" |
| You get | The changed lines, as a diff (`-` removed, `+` added) | A list of the new links, with their titles |
| Use it for | A page that gets edited: a casting page, rules, an "about" page, a status page | A list that grows: a news homepage, an archive, search results |
| Why | The page is mostly the same text with small edits | Lists change constantly. Text mode would fire whenever anything moves. Links mode ignores that and only reports links it hasn't seen before |

From the sites already in the config: the San Marino casting page is `text`, and the `rtl.lu` pages are `links`.

**Links mode and changed titles:** by default `links` only looks at URLs, so if an existing link gets a new title
(a live blog that keeps its URL), you won't hear about it. Add `notify_title_changes: true` and you also get
`✏️ old title → new title`. Leave it off for pages whose link text contains counters or times
("Comments (12)", "2 hours ago"), because those change constantly. Links that have no text are ignored, and
`keywords` also apply (the old or the new title must match).

**Tables in `mode: text` (`table_rows: true`):** by default every table cell is its own line, so when a casting
list moves "Name" from 11:00 to 11:30 you only see `-Name` and `+Name`, without the times. With `table_rows: true`
each row stays on one line (`11:00 | Name`), so the diff shows the time too. Turning it on for an existing watcher
gives one "page changed" message, because the stored text has a different format.

**`mode: matches` (for pages that hide their items):** some pages only contain a list of IDs in the source, even
inside HTML comments (`<!--<box box-left 51895483 file>-->`), and load each item from its own URL. Give a regex with
one `(...)` group around the ID (`pattern:`) and the URL of one item with `{id}` in it (`item_url:`). New IDs are
reported with the item's title and first link, and `keywords` are checked against the item's text. The first run
is a silent baseline. If an item can't be opened (error), it's retried on the next run; a 404 is skipped for good.

`mode: matches` reads the raw page source, so it also works when the items are only data inside a `<script>`
(the page builds the visible list with JavaScript). Use named groups in the pattern: `(?P<title>...)`,
`(?P<url>...)` and optionally `(?P<id>...)`. Escapes like `ę` and `\/` are decoded for you.

Not sure? Run `python monitor.py test "Name"` and look at what comes out.

---

## Keywords (only report what you care about)

Add `keywords:` to a watcher and it only reports items that match.

```yaml
- name: Dutch news
  type: rss
  url: https://example.nl/rss
  keywords: ["songfestival", "eurovisie"]
```

**Endings are handled for you.** A keyword matches any word that *starts* with it, so one entry covers all the grammatical endings:

| you write | it catches |
|---|---|
| `eurovizij` | Eurovizija, Eurovizijos, Eurovizijoje, Euroviziją… |
| `songfestival` | Songfestival, Songfestivalfinale, Songfestivals… |

Case and accents are ignored (`eirovizij` matches `EIROVĪZIJA`). Different *spellings* of a word still need their own entry, for example `eurovizij` and `eirovizij`.

**Several words that must ALL be there:** join them with `+`. `"festival+kenges"` matches a title that has a word
starting with `festival` **and** a word starting with `kenges`, in any order, with anything in between
("Festivali i 65-të i Këngës", "Festivalin e Këngës 65"). Each part follows the normal rules, so `"=esc+2026"` needs
the exact word ESC and a word starting with 2026. A list means "any of these": `["=fest", "festival+kenges"]`
matches `Fest 65` or any festival-and-kenges title. A fixed phrase with spaces (`"festivali i kenges"`) also works.

Two special forms:
- `=esc` matches only the exact word, so `ESC` but not `escalator`. Use it for short abbreviations.
- `*songfestival` also matches inside a word, such as `Eurosongfestival`.

Check a keyword before you use it: `python monitor.py match "eurovizij" "Lietuva Eurovizijoje"`.

What the keywords look at:
- **RSS:** title, summary, full content and categories of each new item.
- **Page, `mode: links`:** link text and the link's URL.
- **Page, `mode: text`:** only changed lines containing a keyword count.
- **WordPress:** title, URL and (with `diff: true`) the text.
- **Sitemap:** the page URL (so only useful when the slug contains the word).

Items that don't match are still remembered as seen, so adding keywords later never causes old items to be sent.

---

## Seeing what changed on a page

**WordPress watchers: `diff: true`.** The monitor asks the API for the content as well, and the message shows the changed lines under each modified item, or the beginning of the text for a new one. If an item's `modified` date changed but the visible text didn't, it says so. This works for posts, pages and most custom post types. Media items have no `content`, so for those the diff only works if you set `diff_fields: ["description", "caption"]`. The first change after switching it on may say "no earlier snapshot", and from the next change on you get proper diffs.

**Sitemap watchers: `snapshots: true`.** When the sitemap says a URL is new or changed, the monitor opens that page and compares its text with the saved version. Notes:
- On the first run it silently saves the 30 most recently updated pages as the starting point (`snapshot_limit`). After that, every new or changed page is saved when it's seen.
- A changed page without an earlier snapshot shows "no earlier snapshot, saved now". New pages show the start of their text.
- By default it ignores `header`, `footer`, `nav` and `aside`, so a changing sidebar doesn't count. Use `snapshot_selector: "article"` (or `main`) to focus on the real content.
- Only the first ~5,000 characters of a page are compared.
- It fetches at most 15 pages per run (`snapshot_max_fetch`) to stay polite. With `snapshot_urls: ["/news/"]` you can limit which URLs are snapshotted.
- You get one message per child sitemap, so the diffs have room.

---

## PDF files

```yaml
- name: Armenia terms and conditions
  type: pdf
  url: https://eurovision.am/media/landing/TermsAndConditions_en.pdf
```

Every run, the monitor downloads the file and reads its text. If the text changed, you get the changed **sentences** (`-` removed, `+` added), plus the file size and page count. The comparison is done per sentence, so a paragraph that is only re-wrapped, or moves to another page, doesn't count as a change. Page numbers and headers or footers that repeat on the pages are ignored automatically.

- **File replaced, same text:** you get "PDF file changed (text identical)". This happens when someone re-exports the file. Add `text_only: true` to ignore such changes. With `keywords:` set, they're ignored automatically.
- **Scanned PDFs** (a photo of a page) have no text to read. You can only be told that the file changed. `python monitor.py test "Name"` shows how much readable text the monitor finds, and the log says so on the first run.
- **Lines that always change** (like "Printed on …") can be skipped with `ignore_patterns: ['^Printed on']`. Matching sentences are left out of the comparison.
- **New filename** (like `..._2026.pdf`): a watcher on the old URL won't notice. Watch the page that links to the PDFs with a `page` watcher in `mode: links` instead.
- Other options: `max_size_mb` (default 25), `max_pages` (default 300), `snapshot_max_text` (how much text is stored, default 60,000 characters), `keywords`.
- This needs the `pypdf` library, which is in `requirements.txt`. If you update `monitor.py`, update `requirements.txt` too.

---

## Pages and files that come and go (404)

Some URLs answer "404 not found" until something is published, and sometimes disappear again. Normally a 404 counts as an error: you'd get a "check failed" alert, and when the file finally appears it would be recorded silently as the starting point. For these URLs, add `allow_missing: true`:

```yaml
- name: Armenia terms (no language suffix)
  type: pdf
  url: https://eurovision.am/media/landing/TermsAndConditions.pdf
  enabled: true
  allow_missing: true
```

Then a 404 (or 410) is a normal state, and you get:
- **🟢 Now online** when the page or file appears, with its size and the start of its text.
- **🔴 No longer online** when it disappears again. Add `notify_removed: false` if you don't want those.
- **🟢 Online again** when it comes back. It says whether the content is the same as the last version it saw, and if not, the usual diff follows in a second message.
- Nothing while it stays missing.

It works for `pdf` and `page` watchers. Only a real 404/410 counts as "missing". Other errors (403, 500, timeouts) are still reported as failures. If a site shows a "not found" page with a normal 200 status instead of a real 404, the monitor can't tell it's missing. Also keep in mind that the monitor checks every ~15 minutes, so a file that is only online for a few minutes can be missed.

---

## Display names (`label`)

The name above each Discord message is the watcher's `name`. To show something different, add `label:`:

```yaml
- name: Romania sitemap
  label: 🇷🇴 Romania · sitemap
```

Keep `name` unchanged, since it decides the file in `state/`. If you do rename a watcher, rename its state file to match (the name in lowercase with dashes) or accept one silent re-baseline.

---

## Choosing a part of a page (selector)

**Bookmarklet (hover-to-pick, like the Chrome extension).** Create a new bookmark in your browser, name it "Pick selector", and paste the entire contents of `bookmarklet.txt` as its URL. On any page, click it, hover until the element you want is highlighted, click, and the CSS selector is copied to your clipboard. Paste it as `selector:` in `config.yaml`.

**Or DevTools:** right-click the element → Inspect → right-click the highlighted node → Copy → Copy selector.

Useful page options: `exclude_selectors: ["footer", ".cookie-banner"]` removes noisy parts, and `ignore_patterns: ["^Updated \\d+"]` ignores lines matching a regex. If `test` shows an empty page, the content is loaded by JavaScript, which a plain fetch can't see.

Other commands: `python monitor.py list`, `python monitor.py run --dry-run` (print what would be sent, send nothing).

---

## Thumbnails for WordPress (`thumbnails: true`)

Add `thumbnails: true` to a `type: wordpress` watcher and the message gets a small picture at the top right
(click it in Discord to enlarge):

- **Media watcher** (`/wp-json/wp/v2/media`): the first new picture (jpg, png, gif or webp). The "medium" size is used
  when WordPress made one, so Discord doesn't have to load a huge original. PDFs, video and SVG get no picture.
- **Posts and pages**: the featured image of the first new item that has one (one extra small request per item, at most 3
  per run).

A message holds one thumbnail, so with several new pictures you see the first one. If the picture can't be found
the message is sent without it.

---

## Translating headlines (`translate`)

Add `translate: en` under `defaults:` in `config.yaml` and every new headline gets an English line below it:

```
🆕 [Водещата на „Евровизия“ …](link)
🌐 *The host of "Eurovision" …*
```

For RSS items the translation is shown in bold at the top, followed by an empty line and the original summary.

- It applies to new links (`mode: links` and `matches`), RSS items and WordPress posts. Text diffs, PDFs and sitemap URLs are not translated.
- Switch it off for one watcher with `translate: false` (do this for sites that are already in English: they would only cost characters). Another language: `translate: de`.
- It uses Google Cloud Translation (Basic). Put the API key in the repo secret `GOOGLE_TRANSLATE_API_KEY` (and add the env line to the workflow, see `.github/workflows/monitor.yml`).
- Only headlines of items that are about to be reported are translated, and every translation is remembered in `state/_translations.json`, so the same headline is never paid for twice. A headline that is already in the target language gets no extra line.
- Failure is harmless: no key, a quota error or an outage just means messages without the translation line. After a key/quota error, no more requests are made during that run.
- Test the setup: `GOOGLE_TRANSLATE_API_KEY=... python monitor.py translate "Нова песен"`.

---

## Good to know

- **Feeds with one item per page.** Some WordPress feeds only show the latest post (`?paged=2` shows the one before). Add `pages: 3` to an RSS watcher and each run reads the first 3 pages. A page that fails or is empty just stops the loop.
- **Some sites block GitHub's servers.** Sites behind Cloudflare or similar protection sometimes answer data-center traffic with 403. You'll get a Discord alert after 3 failed runs in a row (`fail_alert_after`) and another when it recovers. A site that blocks it consistently can't be monitored this way.
- **Failure alerts are short and grouped.** The cause is shown in one line ("⏱️ Timed out (no answer within 30 s)", "🚫 HTTP 403: access denied", "🌐 Site not found (DNS error)"), and when several watchers of the same site fail with the same cause in the same run you get ONE message listing them (and one "working again" message). The full technical error is in the Actions log. For a flaky site, raise the threshold on that watcher: `fail_alert_after: 8` (about an hour at a 7-minute rhythm).
- **A site that can't be reached at all** (DNS error, connection refused, SSL problem, no connection within the timeout) is tried once per run: the other watchers on the same site are skipped for that run without counting as failures, so the run stays fast.
- **Sitemap `lastmod` quirks.** Some sites bump every `lastmod` on every request or leave it out. Then you'll see constant "changes" (try `ignore_urls`) or none.
- **Big sitemap indexes.** `max_children: 80` per watcher by default. Use `include_children` / `exclude_children` (regexes) to focus.
- **Discord outage?** If a message can't be delivered, the change isn't marked as seen, so it's reported on the next run.
- **Idle repos.** GitHub pauses scheduled workflows in public repos after 60 days without repository activity. The monitor commits a small heartbeat file weekly to avoid that. If GitHub does disable it, one click on "Enable workflow" in the Actions tab brings it back.
- **State size.** Snapshots live in `state/` in your repo. They're capped (about 100 pages per sitemap watcher, 50 texts per WordPress watcher).
- Be polite: intervals in minutes, not seconds.

---

## Updating an existing install

Replace `monitor.py`, `requirements.txt` (and this README) in your repo: *Add file → Upload files*, drop the new files with the same names, and commit. Do **not** replace your `config.yaml`; copy what you need from `config-examples.yaml`. Existing state files keep working. The next scheduled run uses the new code.
