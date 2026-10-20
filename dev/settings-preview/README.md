# /settings preview

A local page that renders the real output of `settings_ui.py` the way
the Discord client draws it, and lets you click through it, so a change
to the `/settings` pages can be seen in seconds: edit `settings_ui.py`,
save, and the page re-runs your clicks by itself. No bot token, no
Discord, no network; Python 3.10+ with PyYAML (the package's own
requirement), nothing else to install.

```bash
python3 dev/settings-preview/serve.py            # http://127.0.0.1:8740/
python3 dev/settings-preview/serve.py --port 0   # any free port
MUT_DIR=/path/to/tree python3 dev/settings-preview/serve.py
```

Open the address it prints, then press **/settings** (or type
`/settings` in the message box). Ctrl-C stops it.

## What you get

- **The client.** A Discord window with the server list, the channel
  list, the channel's messages and the message box; or a 390 px phone
  (**Mobile 390**). **Dark** (default) and **Light** themes.
- **Who and where.** The viewer switcher (owner Zach, server admin
  Riley, member Lee) at the top; the server, channel, thread or the
  bot's DM in the sidebar or under **Where**. Changing either starts a
  new conversation; the fake world keeps its settings.
- **Entry points.** `/settings`; **Apps › Why no reply?** when you
  hover a message (or **Why no reply? (last message)**); the join
  notice's **Configure** button in the bot's DM.
- **Clicking.** Every button, select and modal goes to the real
  `settings_ui.handle()` with the interaction Discord would send (the
  clicked message's components, resolved users, roles and channels),
  and its answer is applied like the client does: an update replaces
  the message, a new message appears below with "Only you can see
  this", a modal opens over the window, a failure shows "This
  interaction failed".
- **The inspector.** For the selected message: components out of 40,
  text out of the 3,600-character budget `settings_ui.py` keeps, the
  answer time, and every problem the offline suites' checker
  (`tests/settingsui/crawl.py`: Discord's limits and the copy rules)
  finds, plus the page's own checks (custom_id over 100 characters,
  more than 5 in a row, a select sharing its row, over 25 options,
  label, placeholder and description lengths, duplicate ids, modal
  title and size). Controls that break a limit get a red dashed
  outline. The raw response and interaction JSON fold out below.
  `settings_ui`'s log (a traceback when it answered with its apology)
  shows there too.
- **Re-run.** The steps you took are listed; **Re-run** (and every save
  of `settings_ui.py` or `muse_settings.py` while **Live** is on) starts
  a fresh fake world and replays them, stopping where a control is
  gone. Reloading the browser does the same. A file that doesn't import
  shows its traceback instead, until it is fixed; the server never
  needs a restart for these two files.

## What is faked

Everything comes from the offline suites' fixtures,
`tests/settingsui/harness.py` (`FakeCtx`), so the preview and the tests
see the same world:

- Servers: Muse Lounge (1,240 members, a Staff category, #lobby the
  bot can't post in, #hidden it can't read, a thread in #muse-chat, 74
  filler channels), Study Group (9), Pixel Plaza (48,213), and 60
  filler servers that only show in the server lists.
- People: owner Zach, server admin Riley (Manage Server in whichever
  server you look at), member Lee, and Sam, Kai, Ana, spam1, Bob,
  OtherBot; a role, @Mods, in Muse Lounge.
- The ledger of refused messages behind **Why no reply?**; the channel
  history shows one message for each.
- Settings: a `config.yaml` plus an owner's and an admin's change in
  `config.discord.yaml`, in a temp dir removed when the server stops
  (Ctrl-C or SIGTERM). Writes go through `muse_settings`, as the
  monitor's `SettingsCtx` does. **Fake world** switches to a plain
  `config.yaml` or an unreadable overlay; **Reset world** starts over.
- The clock stands still at the fixtures' noon (2026-10-19), and undo
  group ids count up from 1, so the same clicks always give the same
  pages.

## Known differences from the real client

- Look: colours, sizes and spacing follow Discord's classic dark and
  light themes by eye; the gg sans font is not bundled (Noto Sans or
  Helvetica stands in). Judge layout and wording here; check the final
  look on a real client (`e2e/README.md`, "Manual checklist:
  /settings").
- Markdown covers what `/settings` uses and the common rest (bold,
  italic, underline, strike, spoilers, inline and block code, `#`
  `##` `###` headers, `-#` subtext, lists, quotes, masked and bare
  links, user, role, channel and command mentions, timestamps), not
  every corner of Discord's parser. Custom emoji show as `:name:`.
- Thumbnails, media galleries and files show as placeholders (nothing
  is fetched). Link buttons and links don't open anything; a link to a
  message in the visible channel scrolls to it.
- User selects list the fixture people (in a DM only you and the bot,
  as Discord does); there is no real member search.
- Discord's own refusals are not simulated: a page that breaks a limit
  is drawn anyway and flagged in the inspector, where Discord would
  answer "This interaction failed".
- Dismissing an ephemeral message hides it until the next Re-run.
- One fake world serves every tab; two tabs clicking at once share it.
