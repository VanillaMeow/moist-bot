# moist-bot

**moist-bot** is my personal Discord bot "framework" with a bunch of random features.<br>
Currently, it also powers **Fleabot**, the bot for the [Fleasion](https://github.com/fleasion/Fleasion) Discord.

***Why is it called moist-bot?***
<details>
<summary><i>See here</i></summary>

> On June 2021, 15 year old me decided I wanted to make a Discord bot that replies with "water" when you say "water". From there on out, I've been expanding this bot's codebase to what it is today.
>
> I've decided to keep the name around as it holds sentimental value to me.<br>
> After all, this was my real introduction to Python, GitHub, and programming itself.

</details>

## Inviting

This is not meant to be a public bot. An invitation link does not exist.

## Running

I would rather not people run an instance of this bot, but here goes anyway:

>[!Important]
>This bot is **ran exclusively from source** with `uv`.

You need to have the following software installed:

- [Git](https://git-scm.com/install/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

```sh
git clone https://github.com/VanillaMeow/moist-bot.git
cd moist-bot

# Install all dependencies
uv sync --locked

# Create the initial database
uv run alembic upgrade head

# IMPORTANT: Set your bot token in ".env"
# You can rename a copy of ".env.example" to ".env"
cp .env.example .env

# Finally, run the bot
uv run app
```

## Notes

- Some background features of moist-bot have been copied or modified from [RoboDanny](https://github.com/Rapptz/RoboDanny) for ease of development. Credit is (hopefully) given where due.
