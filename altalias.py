# altalias - A maubot that lets users publish alternate aliases in rooms.
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Set, Type, Dict, NamedTuple, Pattern, List, Optional
from types import FrameType
from contextlib import contextmanager
import signal
import html
import re

from mautrix.types import RoomID, RoomAlias, EventType, CanonicalAliasStateEventContent
from mautrix.errors import MNotFound, MForbidden, MatrixRequestError
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from maubot import Plugin, MessageEvent
from maubot.handlers import command


class RoomInfo(NamedTuple):
    formats: List[Pattern]


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("command")
        helper.copy("admins")
        helper.copy("require_lowercase")
        helper.copy("rooms")


def raise_timeout(sig: signal.Signals, frame_type: FrameType) -> None:
    raise TimeoutError()


@contextmanager
def timeout(time: float = 1) -> None:
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, time)
    try:
        yield
    finally:
        signal.alarm(0)


class AltAliasBot(Plugin):
    _command: str
    _aliases: Set[str]
    _rooms: Dict[RoomID, RoomInfo]

    async def start(self) -> None:
        self.on_external_config_update()

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        self._command = self.config["command"][0]
        self._aliases = set(self.config["command"])
        self._rooms = {}
        for room_id, info in self.config["rooms"].items():
            self._rooms[room_id] = RoomInfo(formats=[])
            for pattern in info.get("formats", []):
                try:
                    self._rooms[room_id].formats.append(re.compile(pattern))
                except re.error:
                    self.log.warning("Failed to compile pattern %s in room %s", pattern, room_id)

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    def save_rooms(self) -> None:
        self.config["rooms"] = {
            room_id: {
                "formats": [regex.pattern for regex in info.formats]
            } for room_id, info in self._rooms.items()
        }
        self.config.save()

    @command.new(lambda self: self._command, aliases=lambda self, val: val in self._aliases,
                 help="Manage alternate aliases")
    async def altalias(self, evt: MessageEvent) -> None:
        pass

    @staticmethod
    def _get_localpart(alias: RoomAlias) -> str:
        if len(alias) == 0:
            raise ValueError("Alias is empty")
        elif alias[0] != "#":
            raise ValueError("Aliases start with #")
        try:
            sep = alias.index(":")
        except ValueError as e:
            raise ValueError("Alias must contain domain separator") from e
        if sep == len(alias) - 1:
            raise ValueError("Alias must contain domain")
        return alias[1:sep]

    @classmethod
    def _localpart_matches(cls, alias: RoomAlias, equal_to: str) -> bool:
        try:
            localpart = cls._get_localpart(alias)
            if equal_to == localpart:
                return True
        except ValueError:
            pass
        return False

    async def _validate_alias(self, evt: MessageEvent, alias: RoomAlias) -> bool:
        try:
            localpart = self._get_localpart(alias)
        except ValueError:
            await evt.reply("That is not a valid room alias")
            return False
        if not localpart.islower() and self.config["require_lowercase"]:
            await evt.reply("That alias localpart is not in lowercase")
            return False

        try:
            alias_info = await self.client.get_room_alias(alias)
        except MNotFound:
            await evt.reply("That alias does not exist")
        except Exception:
            await evt.reply("Failed to get alias info")
        else:
            if alias_info.room_id == evt.room_id:
                return True
            await evt.reply("That alias does not point to this room")
        return False

    async def _get_existing_aliases(self, evt: MessageEvent
                                    ) -> Optional[CanonicalAliasStateEventContent]:
        try:
            existing_event = await self.client.get_state_event(evt.room_id,
                                                               EventType.ROOM_CANONICAL_ALIAS)
        except MNotFound:
            existing_event = CanonicalAliasStateEventContent()
        except MatrixRequestError as e:
            await evt.reply(f"Failed to get current aliases: {e.message}")
            return None
        except Exception:
            self.log.exception(f"Failed to get m.room.canonical_alias in {evt.room_id}")
            await evt.reply("Failed to get current aliases (see logs for more details)")
            return None
        return existing_event

    def _is_allowed(self, room_id: RoomID, alias: RoomAlias,
                    existing_event: CanonicalAliasStateEventContent) -> bool:
        try:
            cfg = self._rooms[room_id]
        except KeyError:
            localpart = self._get_localpart(alias)
            if self._localpart_matches(existing_event.canonical_alias, localpart):
                return True
            for existing_alias in existing_event.alt_aliases:
                if self._localpart_matches(existing_alias, localpart):
                    return True
        else:
            with timeout(max(len(cfg.formats) * 0.5, 2)):
                for regex in cfg.formats:
                    if regex.fullmatch(alias):
                        return True
        return False

    async def _publish_aliases(self, evt: MessageEvent, alias: str,
                               content: CanonicalAliasStateEventContent) -> None:
        content.alt_aliases.append(alias)
        try:
            await self.client.send_state_event(evt.room_id, EventType.ROOM_CANONICAL_ALIAS,
                                               content)
        except MForbidden:
            await evt.reply("I don't have the permission to publish aliases :(")
        except MatrixRequestError as e:
            await evt.reply(f"Failed to publish alias: {e.message}")
        except Exception:
            self.log.exception(f"Failed to publish alias {alias}")
            await evt.reply("Failed to publish alias (see logs for more details)")

    @altalias.subcommand("publish", aliases=["add"], help="Publish an alias from your server in the"
                                                          " alternate aliases of this room.")
    @command.argument("alias", pass_raw=True, required=True)
    async def add_alias(self, evt: MessageEvent, alias: RoomAlias) -> None:
        if not await self._validate_alias(evt, alias):
            return

        existing_content = await self._get_existing_aliases(evt)
        if existing_content is None:
            return
        elif alias in existing_content.alt_aliases:
            await evt.reply("That alias is already published in this room")
            return

        if not self._is_allowed(evt.room_id, alias, existing_content):
            await evt.reply("That alias is not allowed in this room")
            return

        await self._publish_aliases(evt, alias, existing_content)

    @altalias.subcommand("allow", help="Add a regex for matching allowed alternate aliases")
    @command.argument("regex", pass_raw=True, required=True)
    async def allow_format(self, evt: MessageEvent, regex: str) -> None:
        if evt.sender not in self.config["admins"]:
            powers = await self.client.get_state_event(evt.room_id, EventType.ROOM_POWER_LEVELS)
            if (powers.get_user_level(evt.sender)
                    < powers.get_event_level(EventType.ROOM_CANONICAL_ALIAS)):
                await evt.reply("You don't have the permission to manage aliases in this room")
                return
        try:
            room = self._rooms[evt.room_id]
        except KeyError:
            room = RoomInfo(formats=[])
            self._rooms[evt.room_id] = room
        room.formats.append(re.compile(regex))
        self.save_rooms()
        await evt.reply(f"Added <code>{html.escape(regex)}</code> as an allowed alias format",
                        allow_html=True, markdown=False)

    @altalias.subcommand("allowed", help="View allowed alternate alias formats")
    async def allowed_formats(self, evt: MessageEvent) -> None:
        try:
            room = self._rooms[evt.room_id]
        except KeyError:
            await evt.reply("This room does not have special alias rules. Aliases with the same "
                            "localpart as any of the existing aliases can be published.")
        else:
            allowed = "".join(f"<li><code>{html.escape(regex.pattern)}</code></li>"
                              for regex in room.formats)
            await evt.reply("<p>This room allows aliases matching "
                            "the following regular expressions:</p>"
                            f"<ul>{allowed}</ul>", markdown=False, allow_html=True)
