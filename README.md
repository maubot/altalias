# altalias
A [maubot](https://github.com/maubot/maubot) that lets users publish alternate aliases in rooms.

## Usage
By default, the bot will let users add any alias where the localpart is the same as any existing
published alias. Optionally, room admins can use `!altalias allow` to specify custom regular
expressions to define what aliases are allowed.

### Commands
* `!altalias publish <alias>` - Publish an alternate alias for the room.
* `!altalias allow <regex>` - Add a regex for matching allowed alternate aliases.
* `!altalias allowed` - View allowed regexes.
