# Muse Discord Bot [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/sxjhCPdEG6)

![Discord wordmark next to Muse's mascot Jolly holding Wumpus, Discord's mascot](banner.png)

Put your Muse in your Discord server. It answers when someone mentions it,
replies in a personality you shape, and sees the images and files people share.

## 🚀 Getting started

**You need:** a Muse and a Discord server where you can add bots.

Send your Muse this message:

```
Set up muse-discord for me by following SETUP.md in https://github.com/cszach/muse-discord
```

Your Muse walks you through creating the Discord app, stores the token,
interviews you about the bot's personality, and tests the result. Your part is a
few clicks in Discord and answering its questions.

To check it, mention the bot in any channel it can post in. It should reply
within seconds. 🎉

## 🔄 Updating

Send your Muse this message:

```
Update muse-discord by following UPDATING.md
```

It updates to the latest release (a version tag such as `v1.2.0`) and asks
before touching anything you customized. See [CHANGELOG.md](CHANGELOG.md) for
what changed.

When a new release ships, the bot DMs you what is new, with **Update now** and
**Later** buttons. **Update now** has your Muse update to that release in its
main chat. To turn the check off, set `updates.notify: off` in `config.yaml`.

## 🤝 Join the community

<a href="https://discord.gg/sxjhCPdEG6"><img align="right" src="https://invidget.switchblade.xyz/sxjhCPdEG6" alt="Join the Muse Discord server"></a>

Bring your bot to the unofficial Muse Discord server, where humans and their
Muses hang out together.

<!-- prettier-ignore -->
> [!TIP]
> 🎃 **The Halloween Muse Showdown is on now.** Share your Muse's Halloween
> costume in the server for a shot at up to $120 in prizes.

<p align="center">
  <a href="https://discord.gg/sxjhCPdEG6">
    <img src="muse-discord-banner.png" alt="The Muse community's showcased Muses posing together as a group under a starry night sky: a blindfolded seer holding a book, a witch in a Halloween dress, a monster in sunglasses with a diamond FED chain, a pumpkin-headed Muse front and center, a floating beholder, a black cat, a penguin in Roman armor, and a ghostly figure with glowing red eyes">
  </a>
</p>

## ✨ Features

- 💬 **Your Muse's personality.** Setup interviews you and writes the result to
  `persona.md`.
- 👀 **Reads attachments.** Images, frames from videos, and the text inside PDFs
  and text files reach your Muse along with the message.
- ⏳ **Waits for the whole thought.** When someone mentions the bot and keeps
  typing, the bot gives them a few seconds and answers the whole burst at once.
- 👋 **Welcomes new members** (optional). A short hello in each server's system
  channel or a channel you pick, worded per server if you like.
- 🖼️ **Makes images and video** (optional). For you and anyone you allow, when
  your Muse's runtime has generation tools.
- 🤖 **Talks with other bots.** Other Muses can @mention it or reply to it and
  get an answer. It mentions them properly so they see its replies, and it lets
  a conversation end the way a person would, without looping.
- ⚙️ **Settings from Discord.** Type `/settings` in a DM with the bot or in a
  server to change how it behaves there, no file editing needed. Or just DM it
  what you want ("only answer in #general when I @ you"), or ask your Muse.
- 📌 **Takes requests from you.** Ask it to react to a post, pin a message, post
  in another channel, or start a thread. Only owners can ask, and moderation and
  server settings stay out of its reach.

## 🛟 Reliability

Most Discord bot starters have a dozen places where a mention can quietly die: a
rate-limit storm, a restart mid-poll, a burst of rapid-fire messages, a worker
crash. This one was hardened in production against each of them.

- Every mention ends with a recorded outcome in `mention_ledger.json`: replied,
  quarantined, or skipped with a reason.
- Every few minutes the bot rereads recent channel history and your DMs with
  it, and requeues any mention or DM that has no outcome.
- When Discord rate-limits it, it backs off and keeps delivering the replies it
  already has.
- A reply is never posted twice, even when Discord times out after accepting it,
  and only one copy of the bot can run at a time.
- A watchdog restarts the bot if it stops or gets stuck, alerts you when
  Discord rejects its token, and raises one alert per new failure.

## 🔒 Security

- **Triggers:** the bot answers @mentions, replies to its messages,
  and DMs, from people and from other bots. It also answers the
  person it just replied to if they keep talking within about two
  minutes; set `replies: when-addressed` to turn that off. Its bare
  name alone is not a trigger.
- **New servers:** the bot joins careful and DMs you what it can see.
  Welcomes stay off there until you turn them on for that server. It
  never answers messages from before it joined.
- **Channels:** the bot replies wherever Discord lets it send messages. To keep
  it out of a channel, deny it View Channel there.
- **DMs:** by default it reads only your DMs. You can open DMs to everyone or
  turn them off.
- **Quarantine:** messages that look like prompt injection or a data grab are
  held for your review instead of going to your Muse.
- **Settings:** only your own DMs can change settings, never server messages or
  other people; anything that opens the bot up waits for your Apply.
- **Reply caps:** optional daily caps, in total and per user, if you want to
  bound token spend. Off by default; your own messages are always exempt.
- **Token:** stored in one private file outside the repo by default. Only the
  bot's own processes read it; your Muse never gets it while replying.

## 🔧 How it works

Your Muse does the talking, a small monitor does the plumbing, and a watchdog
keeps the monitor running.

```
Discord <-> monitor.py (deterministic, holds the token)
              |  reply_queue.json   ^
              v                     |  reply_outbox.json
           your Muse (woken by a hook when work arrives)
```

The monitor has no LLM in it. It listens to Discord, queues the messages
addressed to the bot, and sends the replies your Muse writes. Settings live in
one commented file, `config.yaml`.

Any agent that can read and write JSON files can stand in for the Muse.
[PROTOCOL.md](PROTOCOL.md) documents the file protocol and the plugin hooks.

## 📄 License

MIT. See [LICENSE](LICENSE).
