# SETUP.md: installing muse-discord

You, the owner's Muse, follow this guide. The owner does five things:
send you the request, make the Discord app, paste its token, click the
invite link, and mention the bot once. Everything else is yours. Never
ask them for IDs, YAML, Developer Mode, intents or a terminal, and
don't ask questions this guide doesn't. Send the owner messages
below as written, filling in the `<...>` parts, one message per step.

The bot's name is your own name, and by default it is simply you.
Never print, repeat or log the bot token: it goes from the owner's
paste straight into `bin/connect-discord.py`'s stdin.

## Step 1: Get the code (you)

```bash
git clone https://github.com/cszach/muse-discord ~/muse-discord
cd ~/muse-discord && bash bin/install.sh
```

Commands below run in that directory (the "package dir"). `install.sh`
checks Python 3.10+ and jq and installs the packages (falling back to
`pip --user`, then a `.venv`). Fix what an `INSTALL FAILED` line says
and rerun it. With `.venv`, run `bin/settings.py` with
`.venv/bin/python3` later; the setup scripts find it themselves.

Already installed (`config.yaml` exists)? Don't onboard again: run
`python3 bin/doctor.py --no-dm` and fix what it reports.

## Step 2: The Discord app (owner)

If your name contains "discord", Discord refuses it: use
"<your name> Bot" in the message instead. Send:

> Let's make your bot's Discord account. It takes about 3 minutes, on a
> computer.
>
> 1. Open https://discord.com/developers/applications and sign in.
> 2. Click **New Application** (it may say **Create App**), type
>    **<your name>**, tick the box, and click **Create**.
> 3. In the left menu click **Installation**. Under **Install Link**
>    choose **None**, then click **Save Changes**.
> 4. Click **Bot** in the left menu. Switch off **Public Bot**, then
>    click **Save Changes**. (Now only you can add it to servers.)
> 5. Still on **Bot**, click **Reset Token**, then **Yes, do it!**
>    (Discord may ask for your password or 2FA code). Click **Copy** and
>    paste it here. It's the bot's password; I'll lock it away.

If your runtime has a secret or secure input field, offer it first ("or
paste it into this secure box"); when it fails, a chat paste is fine.

## Step 3: Connect (you)

Pipe the paste to the script; never put it in an argument:

```bash
python3 bin/connect-discord.py --name "<your name>" <<'EOF'
<the pasted token>
EOF
```

It checks the paste, reads the app and owner from Discord, turns on the
Message Content intent itself, saves the token (mode 600), writes
`config.yaml` (an existing one is kept or backed up), puts your name in
`persona.md`, and prints the invite link. What its exit status means:

- **0**: done. Go on.
- **2**: wrong paste or a reset token. Nothing was saved. Send the
  owner its `PROBLEM:` line as is; it says exactly what to copy
  instead. Then run it again with the new paste.
- **3**: Discord was unreachable. Wait a minute and run it again.
- **4**: saved, but the owner has a portal step. Send each
  `OWNER STEP:` line, and when they say it's done, run it again with
  the saved token: `python3 bin/connect-discord.py --name "<your name>"
  < ~/.config/muse-discord/bot_token`.

Then send (with the `INVITE:` link):

> Got it, and it's stored safely. If you're ever uneasy about it sitting
> in our chat, press **Reset Token** again and paste me the new one.
>
> Now add the bot to your server: click this link, pick your server, and
> click **Authorize**: <invite link>
>
> (No server yet? In Discord, click **+** at the bottom of the server
> list, then **Create My Own**, then come back to the link.)

If the output had a `NOTE: The bot is public` line, they skipped steps
3-4 above; it is safe (it stays quiet in servers they are not in), so
just add that note's two steps to this message.

## Step 4: Wait for the invite (you)

```bash
python3 bin/connect-discord.py --wait 900
```

It says which server the bot joined and how many channels it can read
and post in. If it can post nowhere, send the owner its `PROBLEM:`
line. On a timeout, remind them of the link once.

## Step 5: The worker and the hooks (you)

Render the worker prompt, saying what you can generate (`none`,
`images`, `video`, or `images,video`):

```bash
python3 bin/render-worker-prompt.py --media <what you can make>
```

It writes `worker-prompt.md` (re-rendered on every update, never edited
by hand). Then register these three hooks, all at once, with the
package dir's absolute path for `<pkg>`:

| Script | Every | Deliver to | Wake prompt | Without it |
| --- | --- | --- | --- | --- |
| `hooks/mention-watch.sh` | 5 s | a dedicated worker chat, not the main one | `You are muse-discord's reply worker. Read <pkg>/worker-prompt.md and follow it exactly; the wake payload below is this run's work.` | Nobody gets an answer, and a crashed bot waits up to 10 minutes for a restart. |
| `hooks/watchdog-hook.sh` | 10 min | the owner's main chat | `muse-discord's watchdog raised an alert (payload). Fix it with <pkg>/TROUBLESHOOTING.md, and tell the owner in a sentence or two only if they must do something.` | A stuck bot is never restarted, alerts reach nobody, and new releases are never noticed. |
| `hooks/update-watch.sh` | 60 s | the owner's main chat | `The owner pressed "Update now" on muse-discord's update DM. Update <pkg> to the release in the payload by following <pkg>/UPDATING.md with the owner in this chat. When done, or if you stop early, delete <pkg>/update_request.json and report the version running, or what waits on the owner.` | The bot's **Update now** button does nothing. |

The scripts find the package from their own location (set
`MUSE_DISCORD_DIR` if the runtime copies them). A runtime without hooks
follows the same table with a poller per row (PROTOCOL.md specifies the
queue), or cron for the watchdog.

Save this memory note in your main chat: "muse-discord is installed at
`<pkg>`. To change its settings, read `<pkg>/SETTINGS.md` first. To
update it, follow `<pkg>/UPDATING.md`. To check it, run `python3
<pkg>/bin/doctor.py --no-dm`."

## Step 6: Start it and prove it (you, then owner)

```bash
bash watchdog.sh          # starts the monitor and presence
python3 bin/doctor.py     # checks everything, then DMs the owner
```

`doctor.py` prints `OK`, `NOTE` or `PROBLEM` lines, each problem with
its fix. Fix them and run it again until it ends with "All good." Only
a run with no problem DMs the owner "I'm online" (with the community
invite), so they get it once; they don't need to answer it. Then send:

> Last step: in any channel of your server, type **@<your name> hello**,
> pick the bot from the list that pops up, and send it.

```bash
python3 bin/doctor.py --wait-mention 600
```

On a `PROBLEM`, send the owner its fix in plain words and run it again.
On `OK`, send:

> It works! <your name> is in your server now. It answers when someone
> @mentions it or replies to it, keeps chatting for a couple of minutes
> with whoever it just answered, answers DMs only from you, and never
> shares private things about you. To change anything, DM it in plain
> words ("only answer in #general") or type **/settings**.
>
> Want to meet other Muse owners and their Muses? Join the Muse Agent
> server: https://discord.gg/sxjhCPdEG6

That's the end of setup.

## Changing settings later

The owner DMs the bot in plain words, or uses `/settings` in Discord.
When they ask you instead, follow SETTINGS.md.

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md): what each `PROBLEM` and
watchdog alert means, how to stop the bot, and manual checks.
