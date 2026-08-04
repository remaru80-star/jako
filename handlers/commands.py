import re

from pyrogram import Client, filters
from pyrogram.types import Message

import config
import database as db

INVITE_LINK_RE = re.compile(r"(?:t\.me/(?:joinchat/|\+)|telegram\.me/(?:joinchat/|\+))(\S+)")


def admin_only(_, __, message: Message):
    return message.from_user is not None and message.from_user.id in config.ADMIN_IDS


admin_filter = filters.create(admin_only)


async def resolve_target_chat(client: Client, message: Message):
    """Resolves the channel a /set_source or /set_backup command refers to.

    Supports, in order:
    1. Running the command directly inside the target channel.
    2. Replying to (or forwarding) a message that was forwarded from the channel.
    3. Passing the channel's numeric id as an argument.
    4. Passing an invite link previously bound via /register.
    """
    if message.chat.type in ("channel", "supergroup"):
        return message.chat.id, message.chat.title

    if message.reply_to_message and message.reply_to_message.forward_from_chat:
        chat = message.reply_to_message.forward_from_chat
        return chat.id, chat.title

    parts = message.text.split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()
        if arg.lstrip("-").isdigit():
            chat_id = int(arg)
            try:
                chat = await client.get_chat(chat_id)
                return chat.id, chat.title
            except Exception:
                return chat_id, str(chat_id)

        link_match = INVITE_LINK_RE.search(arg)
        if link_match:
            chat_id = await db.resolve_invite_link(arg)
            if chat_id:
                return chat_id, arg
            return None, (
                "That invite link isn't registered yet. Add the bot to the channel "
                "as admin, then run /register inside the channel first."
            )

    return None, (
        "Couldn't figure out which channel you mean. Either:\n"
        "- run this command inside the channel itself,\n"
        "- reply to a message forwarded from that channel,\n"
        "- pass the channel's numeric id, or\n"
        "- pass an invite link already bound via /register."
    )


def register_handlers(app: Client):

    @app.on_message(filters.command("start"))
    async def start_cmd(client: Client, message: Message):
        await message.reply_text(
            "Nyaa Backup Bot online.\n"
            "Admin commands: /set_source, /set_backup, /register, /status"
        )

    @app.on_message(filters.command("register"))
    async def register_cmd(client: Client, message: Message):
        if message.chat.type not in ("channel", "supergroup"):
            await message.reply_text("Run /register inside the channel you want to register.")
            return
        try:
            invite_link = await client.export_chat_invite_link(message.chat.id)
        except Exception as e:
            await message.reply_text(f"Couldn't export an invite link: {e}")
            return
        await db.register_channel(invite_link, message.chat.id)
        await message.reply_text(f"Registered this channel.\nInvite link: {invite_link}")

    @app.on_message(filters.command("set_source") & admin_filter)
    async def set_source_cmd(client: Client, message: Message):
        chat_id, label = await resolve_target_chat(client, message)
        if chat_id is None:
            await message.reply_text(label)
            return
        await db.set_source_channel(chat_id)
        await message.reply_text(f"Source channel set to: {label} ({chat_id})")

    @app.on_message(filters.command("set_backup") & admin_filter)
    async def set_backup_cmd(client: Client, message: Message):
        chat_id, label = await resolve_target_chat(client, message)
        if chat_id is None:
            await message.reply_text(label)
            return
        await db.set_backup_channel(chat_id)
        await message.reply_text(f"Backup channel set to: {label} ({chat_id})")

    @app.on_message(filters.command("status") & admin_filter)
    async def status_cmd(client: Client, message: Message):
        settings = await db.get_settings()
        await message.reply_text(
            f"Source channel: {settings.get('source_channel_id')}\n"
            f"Backup channel: {settings.get('backup_channel_id')}"
        )
