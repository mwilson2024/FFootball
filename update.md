Fantasy Football App — Update Requests
Users -
Each user has there own settings to adjust rankings such
admin users. make my account a admin wilsonmw an admin account
Data Sources
 Download source data into our DB and use that instead of live-fetching per user
Check first whether the cache already handles this — avoid duplicating logic
Goal: don't make every user re-download the same source data
 Sources settings page: show a real-time preview of how adjusting a source's weight/inclusion affects player rankings (live update as sliders/toggles change)
Auction Draft
 Add an auction setup/configuration flow to help users define their draft strategy for the dynmaic auction price, e.g.:
Prioritize star WR → star QB → star RB → fill in depth( don;t make this default. use somebody else strategy)
Other strategy templates, since not everyone wants the "stars and scrubs" approach
 Let users choose their own priority order / strategy, not just a fixed default
ADFL (Dynasty League)
    make sure draft logic is correct ie i cannot draft another teams player
 Add ability to filter rankings/cheat sheet by rookies/sleeper other tags
 Add "sleeper" tags to players
 Cheat sheet target sync: if a player is targeted on the cheat sheets, that target should show up in the draft room automatically
AUCTION
the ability to remove and add a player from a team. ie reassign because we got it wrong.(admin only)
admin marks auction live and the we start sharing updating to see the player auctioned is updated on all screens with amount etc(could be a push or update call. maybe a flag of some sort) At max 14 players accessing db at once so it should be that bad.
Rosters
 Remove the max bid field/limit from rosters(ADFL ONLY)
Chatbot
 Add a chatbot option powered via OpenAI API (or configurable API) that has context on:
Current roster
Remaining auction bid budget
Current league scoring/points scheme
Roster construction/lineup requirements
Other imported fantasy league information
