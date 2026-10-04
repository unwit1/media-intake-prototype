"""Live metadata search check: run the app's category search for well-known series and flag problems.

Usage:  python tools/metadata_check.py [output.json] [--only CATEGORY] [--more | --third | --fourth | --fifth] [--resume]

For each query it records the top results and flags: the expected title missing
from the top 5, duplicate rows for one work (same normalized title and creator
within a category), and edition/format/box-set rows that should have been folded
into their work. It uses a fresh rights-evidence file, so only the catalogs and
connectors the app searches by default are queried; nothing is downloaded.
Catalogs that need API keys are skipped unless their keys are set.
"""
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_intake.media_resolver import resolve  # noqa: E402
from media_intake.rights_evidence import ensure_rights_evidence  # noqa: E402

QUERIES = [
    ('Books', 'The Stormlight Archive', 'stormlight archive'), ('Books', 'Percy Jackson and the Olympians', 'percy jackson'),
    ('Books', 'Harry Potter', 'harry potter'), ('Books', 'The Wheel of Time', 'wheel of time'),
    ('Books', 'Discworld', 'discworld'), ('Books', 'Mistborn', 'mistborn'), ('Books', 'The Expanse', 'expanse'),
    ('Books', 'Dune', 'dune'), ('Books', 'The Hunger Games', 'hunger games'),
    ('Books', 'A Song of Ice and Fire', 'song of ice and fire'), ('Books', 'Red Rising', 'red rising'),
    ('Books', 'The Dresden Files', 'dresden files'),
    ('Videos', 'Breaking Bad', 'breaking bad'), ('Videos', 'The Office', 'office'), ('Videos', 'Game of Thrones', 'game of thrones'),
    ('Videos', 'Stranger Things', 'stranger things'), ('Videos', 'Doctor Who', 'doctor who'), ('Videos', 'Severance', 'severance'),
    ('Videos', 'The Bear', 'bear'), ('Videos', 'Succession', 'succession'), ('Videos', 'Arcane', 'arcane'),
    ('Videos', 'Star Wars', 'star wars'), ('Videos', 'The Lord of the Rings', 'lord of the rings'),
    ('Videos', 'Toy Story', 'toy story'), ('Videos', 'Mission: Impossible', 'mission impossible'),
    ('Videos', 'John Wick', 'john wick'), ('Videos', 'The Matrix', 'matrix'), ('Videos', 'Alien', 'alien'),
    ('Comics', 'Saga', 'saga'), ('Comics', 'The Walking Dead', 'walking dead'), ('Comics', 'The Sandman', 'sandman'),
    ('Comics', 'Watchmen', 'watchmen'), ('Comics', 'Invincible', 'invincible'), ('Comics', 'Hellboy', 'hellboy'),
    ('Manga', 'One Piece', 'one piece'), ('Manga', 'Berserk', 'berserk'), ('Manga', 'Chainsaw Man', 'chainsaw man'),
    ('Manga', 'Spy x Family', 'spy x family'), ('Manga', 'Frieren', 'frieren'),
    ('Anime', 'Attack on Titan', 'attack on titan'), ('Anime', 'Fullmetal Alchemist', 'fullmetal alchemist'),
    ('Anime', 'Cowboy Bebop', 'cowboy bebop'),
    ('Games', 'The Legend of Zelda', 'zelda'), ('Games', 'Final Fantasy', 'final fantasy'), ('Games', 'Mass Effect', 'mass effect'),
    ('Games', 'The Witcher', 'witcher'), ('Games', 'Persona', 'persona'),
    ('Light novels', 'Re:Zero', 're zero'), ('Light novels', 'Overlord', 'overlord'), ('Light novels', 'Spice and Wolf', 'spice and wolf'),
]
COMICS_PAUSE = 90
# A second, wider set: older and newer works, non-English originals, punctuation,
# numbers, subtitles, one-word and very common titles.
MORE_QUERIES = [
    ('Books', 'The Lord of the Rings', 'lord of the rings'), ('Books', 'The Chronicles of Narnia', 'narnia'),
    ('Books', 'His Dark Materials', 'his dark materials'), ('Books', "The Hitchhiker's Guide to the Galaxy", 'hitchhiker'),
    ('Books', 'Foundation', 'foundation'), ('Books', 'The Witcher', 'witcher'), ('Books', 'Earthsea', 'earthsea'),
    ('Books', 'The Kingkiller Chronicle', 'kingkiller'), ('Books', 'The Dark Tower', 'dark tower'),
    ('Books', 'Outlander', 'outlander'), ('Books', 'Jack Reacher', 'reacher'), ('Books', 'Twilight', 'twilight'),
    ('Books', 'The Maze Runner', 'maze runner'), ('Books', 'Divergent', 'divergent'), ('Books', 'Shadow and Bone', 'shadow and bone'),
    ('Books', 'The First Law', 'first law'), ('Books', 'The Murderbot Diaries', 'murderbot'),
    ('Books', 'The Three-Body Problem', 'three body'), ('Books', 'Hyperion Cantos', 'hyperion'),
    ('Books', 'The Belgariad', 'belgariad'), ('Books', 'Sherlock Holmes', 'sherlock holmes'),
    ('Books', 'Hercule Poirot', 'poirot'), ('Books', 'Cormoran Strike', 'strike'),
    ('Books', 'The Inheritance Cycle', 'inheritance'), ('Books', 'Wings of Fire', 'wings of fire'),
    ('Videos', 'The Sopranos', 'sopranos'), ('Videos', 'The Wire', 'wire'), ('Videos', 'Friends', 'friends'),
    ('Videos', 'Seinfeld', 'seinfeld'), ('Videos', 'The Simpsons', 'simpsons'), ('Videos', 'Better Call Saul', 'better call saul'),
    ('Videos', 'The Mandalorian', 'mandalorian'), ('Videos', 'Ted Lasso', 'ted lasso'), ('Videos', 'Fargo', 'fargo'),
    ('Videos', 'Twin Peaks', 'twin peaks'), ('Videos', 'Star Trek: The Next Generation', 'next generation'),
    ('Videos', 'Black Mirror', 'black mirror'), ('Videos', 'The Last of Us', 'last of us'),
    ('Videos', 'Harry Potter', 'harry potter'), ('Videos', 'Jurassic Park', 'jurassic park'),
    ('Videos', 'Indiana Jones', 'indiana jones'), ('Videos', 'Back to the Future', 'back to the future'),
    ('Videos', 'The Godfather', 'godfather'), ('Videos', 'Pirates of the Caribbean', 'pirates of the caribbean'),
    ('Videos', 'Shrek', 'shrek'), ('Videos', 'Mad Max', 'mad max'), ('Videos', 'Terminator', 'terminator'),
    ('Videos', 'Rocky', 'rocky'), ('Videos', 'Spider-Man', 'spider man'),
    ('Comics', 'Bone', 'bone'), ('Comics', 'Y: The Last Man', 'last man'), ('Comics', 'Preacher', 'preacher'),
    ('Comics', 'Paper Girls', 'paper girls'), ('Comics', 'Monstress', 'monstress'), ('Comics', 'Fables', 'fables'),
    ('Comics', 'Transmetropolitan', 'transmetropolitan'), ('Comics', 'Usagi Yojimbo', 'usagi yojimbo'),
    ('Comics', 'Sin City', 'sin city'), ('Comics', 'Locke & Key', 'locke and key'), ('Comics', 'Maus', 'maus'),
    ('Comics', 'East of West', 'east of west'), ('Comics', 'Ms. Marvel', 'ms marvel'),
    ('Manga', 'Naruto', 'naruto'), ('Manga', 'Vagabond', 'vagabond'), ('Manga', 'Jujutsu Kaisen', 'jujutsu kaisen'),
    ('Manga', 'Dragon Ball', 'dragon ball'), ('Manga', 'Death Note', 'death note'), ('Manga', 'Vinland Saga', 'vinland saga'),
    ('Manga', 'Monster', 'monster'), ('Manga', 'Akira', 'akira'), ('Manga', 'Yotsuba&!', 'yotsuba'),
    ('Manga', 'Oyasumi Punpun', 'punpun'), ('Manga', 'Blue Lock', 'blue lock'), ('Manga', 'Dandadan', 'dandadan'),
    ('Anime', 'Neon Genesis Evangelion', 'evangelion'), ('Anime', 'Mob Psycho 100', 'mob psycho'),
    ('Anime', 'Demon Slayer', 'demon slayer'), ('Anime', 'Steins;Gate', 'steins gate'), ('Anime', 'Haikyu!!', 'haikyu'),
    ('Anime', 'My Hero Academia', 'my hero academia'), ('Anime', 'Ghost in the Shell', 'ghost in the shell'),
    ('Anime', 'Gurren Lagann', 'gurren lagann'), ('Anime', 'Bocchi the Rock!', 'bocchi'), ('Anime', 'Hunter x Hunter', 'hunter x hunter'),
    ('Games', 'Super Mario', 'mario'), ('Games', 'Halo', 'halo'), ('Games', 'Metroid', 'metroid'),
    ('Games', 'Dark Souls', 'dark souls'), ('Games', 'The Elder Scrolls', 'elder scrolls'), ('Games', 'Fallout', 'fallout'),
    ('Games', 'Resident Evil', 'resident evil'), ('Games', 'Kingdom Hearts', 'kingdom hearts'),
    ('Games', 'Pok\u00e9mon', 'pokemon'), ('Games', 'Half-Life', 'half life'),
    ('Light novels', 'Sword Art Online', 'sword art online'), ('Light novels', 'Mushoku Tensei', 'mushoku tensei'),
    ('Light novels', 'The Apothecary Diaries', 'apothecary diaries'), ('Light novels', 'Ascendance of a Bookworm', 'bookworm'),
    ('Light novels', 'Konosuba', 'konosuba'), ('Light novels', 'That Time I Got Reincarnated as a Slime', 'slime'),
]
# Third set: numerals and Roman numerals, apostrophes and ampersands, non-English
# originals, series better known by another name, children's and older classics,
# reboots that reuse a title.
THIRD_QUERIES = [
    ('Books', 'A Series of Unfortunate Events', 'unfortunate events'), ('Books', 'The Expanse', 'expanse'),
    ('Books', 'Artemis Fowl', 'artemis fowl'), ('Books', 'Goosebumps', 'goosebumps'),
    ('Books', 'The Baby-Sitters Club', 'baby sitters club'), ('Books', 'Nancy Drew', 'nancy drew'),
    ('Books', 'The Hardy Boys', 'hardy boys'), ('Books', 'Diary of a Wimpy Kid', 'wimpy kid'),
    ('Books', 'Magic Tree House', 'magic tree house'), ('Books', 'Anne of Green Gables', 'green gables'),
    ('Books', 'Little House on the Prairie', 'little house'), ('Books', 'Redwall', 'redwall'),
    ('Books', 'Dragonriders of Pern', 'pern'), ('Books', 'The Culture', 'culture'), ('Books', 'Dune Chronicles', 'dune'),
    ('Books', 'Ender\'s Game', 'ender'), ('Books', 'The Silo Series', 'silo'), ('Books', 'Mistborn Era Two', 'mistborn'),
    ('Books', 'The Millennium Trilogy', 'millennium'), ('Books', 'Harry Hole', 'harry hole'),
    ('Books', 'Inspector Rebus', 'rebus'), ('Books', 'Alex Cross', 'alex cross'), ('Books', 'Bridgerton', 'bridgerton'),
    ('Books', 'A Court of Thorns and Roses', 'thorns and roses'), ('Books', 'The Wandering Inn', 'wandering inn'),
    ('Videos', 'Star Trek', 'star trek'), ('Videos', 'Battlestar Galactica', 'battlestar galactica'),
    ('Videos', 'Doctor Who Confidential', 'confidential'), ('Videos', 'Law & Order', 'law and order'),
    ('Videos', 'Grey\'s Anatomy', 'grey s anatomy'), ('Videos', 'It\'s Always Sunny in Philadelphia', 'always sunny'),
    ('Videos', 'Avatar: The Last Airbender', 'last airbender'), ('Videos', 'Rick and Morty', 'rick and morty'),
    ('Videos', 'BoJack Horseman', 'bojack'), ('Videos', 'The X-Files', 'x files'), ('Videos', '24', '24'),
    ('Videos', 'Lost', 'lost'), ('Videos', 'House of the Dragon', 'house of the dragon'), ('Videos', 'Dark', 'dark'),
    ('Videos', 'Money Heist', 'money heist'), ('Videos', 'Squid Game', 'squid game'),
    ('Videos', 'The Hunger Games', 'hunger games'), ('Videos', 'Fast & Furious', 'fast'),
    ('Videos', 'Ocean\'s Eleven', 'ocean'), ('Videos', 'Planet of the Apes', 'planet of the apes'),
    ('Videos', 'James Bond', 'bond'), ('Videos', 'Despicable Me', 'despicable me'),
    ('Videos', 'How to Train Your Dragon', 'train your dragon'), ('Videos', 'Rambo', 'rambo'),
    ('Videos', 'Godzilla', 'godzilla'),
    ('Comics', 'Hellblazer', 'hellblazer'), ('Comics', 'Swamp Thing', 'swamp thing'), ('Comics', 'Ghost World', 'ghost world'),
    ('Comics', 'Love and Rockets', 'love and rockets'), ('Comics', '2000 AD', '2000 ad'), ('Comics', 'Astro City', 'astro city'),
    ('Comics', 'Black Hammer', 'black hammer'), ('Comics', 'Something is Killing the Children', 'killing the children'),
    ('Comics', 'Tintin', 'tintin'), ('Comics', 'Asterix', 'asterix'),
    ('Manga', 'Fullmetal Alchemist', 'fullmetal alchemist'), ('Manga', 'Bleach', 'bleach'),
    ('Manga', 'JoJo\'s Bizarre Adventure', 'jojo'), ('Manga', 'Slam Dunk', 'slam dunk'), ('Manga', 'Nana', 'nana'),
    ('Manga', 'Sailor Moon', 'sailor moon'), ('Manga', 'Kingdom', 'kingdom'), ('Manga', 'Tokyo Ghoul', 'tokyo ghoul'),
    ('Manga', 'Ranma 1/2', 'ranma'), ('Manga', 'Kaguya-sama: Love Is War', 'kaguya'), ('Manga', '20th Century Boys', '20th century boys'),
    ('Manga', 'Witch Hat Atelier', 'witch hat atelier'),
    ('Anime', 'Code Geass', 'code geass'), ('Anime', 'Samurai Champloo', 'samurai champloo'),
    ('Anime', 'Made in Abyss', 'made in abyss'), ('Anime', 'Violet Evergarden', 'violet evergarden'),
    ('Anime', 'One Punch Man', 'one punch man'), ('Anime', 'Sailor Moon', 'sailor moon'),
    ('Anime', 'Mobile Suit Gundam', 'gundam'), ('Anime', 'Your Lie in April', 'your lie in april'),
    ('Anime', 'Cyberpunk: Edgerunners', 'edgerunners'), ('Anime', 'Spirited Away', 'spirited away'),
    ('Games', 'Final Fantasy VII', 'final fantasy vii'), ('Games', 'Grand Theft Auto', 'grand theft auto'),
    ('Games', 'The Sims', 'sims'), ('Games', 'Civilization', 'civilization'), ('Games', 'Street Fighter', 'street fighter'),
    ('Games', 'Mortal Kombat', 'mortal kombat'), ('Games', 'Assassin\'s Creed', 'assassin s creed'),
    ('Games', 'Call of Duty', 'call of duty'), ('Games', 'Metal Gear', 'metal gear'), ('Games', 'Tomb Raider', 'tomb raider'),
    ('Games', 'Diablo', 'diablo'), ('Games', 'Baldur\'s Gate', 'baldur s gate'),
    ('Light novels', 'Spice and Wolf', 'spice and wolf'), ('Light novels', 'Haruhi Suzumiya', 'haruhi'),
    ('Light novels', 'Overlord', 'overlord'), ('Light novels', 'Classroom of the Elite', 'classroom of the elite'),
    ('Light novels', 'The Rising of the Shield Hero', 'shield hero'), ('Light novels', 'No Game No Life', 'no game no life'),
]
# Fourth set: international works (K-drama, manhwa, European comics, anime films),
# long-running franchises, remakes sharing a name, numbers and symbols, and book
# series known by a character or a world.
FOURTH_QUERIES = [
    ('Books', 'Percy Jackson', 'percy jackson'), ('Books', 'The Heroes of Olympus', 'heroes of olympus'),
    ('Books', 'Throne of Glass', 'throne of glass'), ('Books', 'The Stormlight Archive', 'stormlight'),
    ('Books', 'Cosmere', 'cosmere'), ('Books', 'Middle-earth', 'middle earth'), ('Books', 'Xanth', 'xanth'),
    ('Books', 'The Realm of the Elderlings', 'elderlings'), ('Books', 'Malazan Book of the Fallen', 'malazan'),
    ('Books', 'The Sword of Truth', 'sword of truth'), ('Books', 'Shannara', 'shannara'),
    ('Books', 'Vorkosigan Saga', 'vorkosigan'), ('Books', 'Honor Harrington', 'honor harrington'),
    ('Books', 'Miss Marple', 'marple'), ('Books', 'Inspector Morse', 'morse'),
    ('Books', 'Discworld City Watch', 'city watch'), ('Books', 'The Thursday Murder Club', 'thursday murder club'),
    ('Books', 'Bosch', 'bosch'), ('Books', 'Dirk Pitt', 'dirk pitt'), ('Books', 'Captain Underpants', 'captain underpants'),
    ('Books', 'Dog Man', 'dog man'), ('Books', 'Warriors', 'warriors'), ('Books', 'Animorphs', 'animorphs'),
    ('Books', 'Fear Street', 'fear street'), ('Books', 'The Lunar Chronicles', 'lunar chronicles'),
    ('Videos', 'Crash Landing on You', 'crash landing on you'), ('Videos', 'Kingdom', 'kingdom'),
    ('Videos', 'Downton Abbey', 'downton abbey'), ('Videos', 'Sherlock', 'sherlock'), ('Videos', 'Peaky Blinders', 'peaky blinders'),
    ('Videos', 'The Crown', 'crown'), ('Videos', 'Westworld', 'westworld'), ('Videos', 'The Boys', 'boys'),
    ('Videos', 'Yellowstone', 'yellowstone'), ('Videos', 'Euphoria', 'euphoria'), ('Videos', 'Shōgun', 'shogun'),
    ('Videos', 'The Witcher', 'witcher'), ('Videos', 'The Handmaid\'s Tale', 'handmaid'),
    ('Videos', 'Planet Earth', 'planet earth'), ('Videos', 'Cosmos', 'cosmos'),
    ('Videos', 'Halloween', 'halloween'), ('Videos', 'Scream', 'scream'), ('Videos', 'Saw', 'saw'),
    ('Videos', 'Die Hard', 'die hard'), ('Videos', 'Ghostbusters', 'ghostbusters'), ('Videos', 'Predator', 'predator'),
    ('Videos', 'X-Men', 'x men'), ('Videos', 'The Avengers', 'avengers'), ('Videos', 'Dune', 'dune'),
    ('Videos', '2001: A Space Odyssey', '2001'),
    ('Comics', 'Blacksad', 'blacksad'), ('Comics', 'Lucky Luke', 'lucky luke'), ('Comics', 'The Incal', 'incal'),
    ('Comics', 'Judge Dredd', 'judge dredd'), ('Comics', 'Scott Pilgrim', 'scott pilgrim'),
    ('Comics', 'Calvin and Hobbes', 'calvin and hobbes'), ('Comics', 'Peanuts', 'peanuts'), ('Comics', 'Nimona', 'nimona'),
    ('Manga', 'Solo Leveling', 'solo leveling'), ('Manga', 'Tower of God', 'tower of god'), ('Manga', 'The Breaker', 'breaker'),
    ('Manga', 'Lookism', 'lookism'), ('Manga', 'Omniscient Reader', 'omniscient reader'),
    ('Manga', 'Cardcaptor Sakura', 'cardcaptor'), ('Manga', 'Inuyasha', 'inuyasha'), ('Manga', 'Fruits Basket', 'fruits basket'),
    ('Manga', 'Mob Psycho 100', 'mob psycho'), ('Manga', 'Hellsing', 'hellsing'), ('Manga', 'Gintama', 'gintama'),
    ('Manga', 'Pluto', 'pluto'),
    ('Anime', 'Cowboy Bebop: The Movie', 'cowboy bebop'), ('Anime', 'Akira', 'akira'), ('Anime', 'Princess Mononoke', 'mononoke'),
    ('Anime', 'Your Name', 'your name'), ('Anime', 'Perfect Blue', 'perfect blue'), ('Anime', 'Trigun', 'trigun'),
    ('Anime', 'Fate/Zero', 'fate zero'), ('Anime', 'Re:Zero', 're zero'), ('Anime', 'Bleach: Thousand-Year Blood War', 'thousand year blood war'),
    ('Anime', 'Puella Magi Madoka Magica', 'madoka'),
    ('Games', 'Sonic the Hedgehog', 'sonic'), ('Games', 'Street Fighter II', 'street fighter ii'),
    ('Games', 'Final Fantasy XIV', 'final fantasy xiv'), ('Games', 'Doom', 'doom'), ('Games', 'Quake', 'quake'),
    ('Games', 'Tetris', 'tetris'), ('Games', 'Portal', 'portal'), ('Games', 'BioShock', 'bioshock'),
    ('Games', 'Uncharted', 'uncharted'), ('Games', 'God of War', 'god of war'), ('Games', 'Monster Hunter', 'monster hunter'),
    ('Games', 'Xenoblade Chronicles', 'xenoblade'),
    ('Light novels', 'Toradora!', 'toradora'), ('Light novels', 'Durarara!!', 'durarara'),
    ('Light novels', 'Oregairu', 'oregairu'), ('Light novels', 'Monogatari', 'monogatari'),
    ('Light novels', 'Goblin Slayer', 'goblin slayer'), ('Light novels', 'The Eminence in Shadow', 'eminence in shadow'),
    ('Light novels', 'DanMachi', 'danmachi'), ('Light novels', 'Log Horizon', 'log horizon'),
]
# Fifth set (200): more of everything, avoiding the earlier sets - classics and
# recent hits, translated literature, YA, crime, romance, sci-fi, kids', reality and
# documentary TV, animation, superhero runs, indie comics, shonen/shojo/seinen,
# isekai, retro and indie games.
FIFTH_QUERIES = [
    # Books (50)
    ('Books', 'The Lord of the Flies', 'lord of the flies'), ('Books', 'The Giver Quartet', 'giver'),
    ('Books', 'The Princess Bride', 'princess bride'), ('Books', 'Rivers of London', 'rivers of london'),
    ('Books', 'The Broken Earth', 'broken earth'), ('Books', 'The Poppy War', 'poppy war'),
    ('Books', 'The Night Angel Trilogy', 'night angel'), ('Books', 'Lightbringer', 'lightbringer'),
    ('Books', 'The Gentleman Bastard Sequence', 'gentleman bastard'), ('Books', 'The Riyria Revelations', 'riyria'),
    ('Books', 'The Black Company', 'black company'), ('Books', 'The Demon Cycle', 'demon cycle'),
    ('Books', 'Sword of Shannara', 'shannara'), ('Books', 'The Elric Saga', 'elric'),
    ('Books', 'Conan the Barbarian', 'conan'), ('Books', 'Tarzan', 'tarzan'), ('Books', 'Barsoom', 'barsoom'),
    ('Books', 'Lensman', 'lensman'), ('Books', 'Old Man\'s War', 'old man s war'), ('Books', 'Bobiverse', 'bobiverse'),
    ('Books', 'The Long Earth', 'long earth'), ('Books', 'Revelation Space', 'revelation space'),
    ('Books', 'Ancillary Justice', 'ancillary'), ('Books', 'The Wayfarers', 'wayfarers'),
    ('Books', 'Imperial Radch', 'radch'), ('Books', 'The Southern Reach', 'southern reach'),
    ('Books', 'Kate Daniels', 'kate daniels'), ('Books', 'Mercy Thompson', 'mercy thompson'),
    ('Books', 'The Southern Vampire Mysteries', 'southern vampire'), ('Books', 'Anita Blake', 'anita blake'),
    ('Books', 'Stephanie Plum', 'stephanie plum'), ('Books', 'Kinsey Millhone', 'millhone'),
    ('Books', 'Jack Ryan', 'jack ryan'), ('Books', 'Lincoln Rhyme', 'lincoln rhyme'),
    ('Books', 'Kay Scarpetta', 'scarpetta'), ('Books', 'Maigret', 'maigret'), ('Books', 'Wallander', 'wallander'),
    ('Books', 'Commissario Montalbano', 'montalbano'), ('Books', 'The Cemetery of Forgotten Books', 'forgotten books'),
    ('Books', 'Neapolitan Novels', 'neapolitan'), ('Books', 'In Search of Lost Time', 'lost time'),
    ('Books', 'The Cairo Trilogy', 'cairo trilogy'), ('Books', 'Junie B. Jones', 'junie b'),
    ('Books', 'Ramona', 'ramona'), ('Books', 'The Boxcar Children', 'boxcar children'),
    ('Books', 'Amelia Bedelia', 'amelia bedelia'), ('Books', 'Paddington', 'paddington'),
    ('Books', 'Winnie-the-Pooh', 'winnie the pooh'), ('Books', 'Mr. Men', 'mr men'), ('Books', 'Thomas the Tank Engine', 'thomas'),
    # TV and film (50)
    ('Videos', 'The West Wing', 'west wing'), ('Videos', 'Mad Men', 'mad men'), ('Videos', 'The Shield', 'shield'),
    ('Videos', 'Deadwood', 'deadwood'), ('Videos', 'Six Feet Under', 'six feet under'), ('Videos', 'Arrested Development', 'arrested development'),
    ('Videos', 'Community', 'community'), ('Videos', 'Parks and Recreation', 'parks and recreation'),
    ('Videos', 'Brooklyn Nine-Nine', 'brooklyn nine nine'), ('Videos', 'Schitt\'s Creek', 'schitt s creek'),
    ('Videos', 'Fleabag', 'fleabag'), ('Videos', 'The Good Place', 'good place'), ('Videos', 'Veep', 'veep'),
    ('Videos', 'Frasier', 'frasier'), ('Videos', 'Cheers', 'cheers'), ('Videos', 'M*A*S*H', 'm a s h'),
    ('Videos', 'Buffy the Vampire Slayer', 'buffy'), ('Videos', 'Firefly', 'firefly'), ('Videos', 'Babylon 5', 'babylon 5'),
    ('Videos', 'Stargate SG-1', 'stargate'), ('Videos', 'Red Dwarf', 'red dwarf'), ('Videos', 'Blackadder', 'blackadder'),
    ('Videos', 'Top Gear', 'top gear'), ('Videos', 'The Great British Bake Off', 'bake off'), ('Videos', 'Survivor', 'survivor'),
    ('Videos', 'Blue Planet', 'blue planet'), ('Videos', 'Making a Murderer', 'making a murderer'),
    ('Videos', 'Bluey', 'bluey'), ('Videos', 'SpongeBob SquarePants', 'spongebob'), ('Videos', 'Futurama', 'futurama'),
    ('Videos', 'Family Guy', 'family guy'), ('Videos', 'South Park', 'south park'), ('Videos', 'Gravity Falls', 'gravity falls'),
    ('Videos', 'Adventure Time', 'adventure time'), ('Videos', 'Steven Universe', 'steven universe'),
    ('Videos', 'Kung Fu Panda', 'kung fu panda'), ('Videos', 'Ice Age', 'ice age'), ('Videos', 'Cars', 'cars'),
    ('Videos', 'Frozen', 'frozen'), ('Videos', 'The Lion King', 'lion king'), ('Videos', 'Paddington', 'paddington'),
    ('Videos', 'Wallace & Gromit', 'wallace and gromit'), ('Videos', 'Toy Story 2', 'toy story 2'),
    ('Videos', 'Blade Runner', 'blade runner'), ('Videos', 'Highlander', 'highlander'), ('Videos', 'RoboCop', 'robocop'),
    ('Videos', 'Child\'s Play', 'child s play'), ('Videos', 'A Nightmare on Elm Street', 'elm street'),
    ('Videos', 'Friday the 13th', 'friday the 13th'), ('Videos', 'The Conjuring', 'conjuring'),
    # Comics (14)
    ('Comics', 'Spawn', 'spawn'), ('Comics', 'The Umbrella Academy', 'umbrella academy'), ('Comics', 'Chew', 'chew'),
    ('Comics', 'Sex Criminals', 'sex criminals'), ('Comics', 'The Wicked + The Divine', 'wicked'),
    ('Comics', 'Hawkeye', 'hawkeye'), ('Comics', 'Daredevil', 'daredevil'), ('Comics', 'The Flash', 'flash'),
    ('Comics', 'Green Lantern', 'green lantern'), ('Comics', 'Doom Patrol', 'doom patrol'),
    ('Comics', 'Concrete', 'concrete'), ('Comics', 'Cerebus', 'cerebus'), ('Comics', 'Elfquest', 'elfquest'),
    ('Comics', 'Strangers in Paradise', 'strangers in paradise'),
    # Manga (24)
    ('Manga', 'Hunter x Hunter', 'hunter x hunter'), ('Manga', 'My Hero Academia', 'my hero academia'),
    ('Manga', 'Demon Slayer', 'demon slayer'), ('Manga', 'Haikyu!!', 'haikyu'), ('Manga', 'Toriko', 'toriko'),
    ('Manga', 'Black Clover', 'black clover'), ('Manga', 'Fire Force', 'fire force'), ('Manga', 'Dr. Stone', 'dr stone'),
    ('Manga', 'The Promised Neverland', 'promised neverland'), ('Manga', 'Ouran High School Host Club', 'ouran'),
    ('Manga', 'Skip Beat!', 'skip beat'), ('Manga', 'Kimi ni Todoke', 'kimi ni todoke'), ('Manga', 'Lone Wolf and Cub', 'lone wolf and cub'),
    ('Manga', 'Golden Kamuy', 'golden kamuy'), ('Manga', 'Blade of the Immortal', 'blade of the immortal'),
    ('Manga', 'Uzumaki', 'uzumaki'), ('Manga', 'Parasyte', 'parasyte'), ('Manga', 'Dorohedoro', 'dorohedoro'),
    ('Manga', 'Hikaru no Go', 'hikaru no go'), ('Manga', 'Initial D', 'initial d'), ('Manga', 'Doraemon', 'doraemon'),
    ('Manga', 'Astro Boy', 'astro boy'), ('Manga', 'Lone Wolf', 'lone wolf'), ('Manga', 'Kaiju No. 8', 'kaiju'),
    # Anime (22)
    ('Anime', 'Naruto Shippuden', 'shippuden'), ('Anime', 'Bleach', 'bleach'), ('Anime', 'Dragon Ball Z', 'dragon ball z'),
    ('Anime', 'Pokémon', 'pokemon'), ('Anime', 'Yu-Gi-Oh!', 'yu gi oh'), ('Anime', 'Digimon Adventure', 'digimon'),
    ('Anime', 'Sword Art Online', 'sword art online'), ('Anime', 'Tokyo Ghoul', 'tokyo ghoul'),
    ('Anime', 'Clannad', 'clannad'), ('Anime', 'Toradora!', 'toradora'), ('Anime', 'K-On!', 'k on'),
    ('Anime', 'Lucky Star', 'lucky star'), ('Anime', 'Serial Experiments Lain', 'lain'),
    ('Anime', 'Paprika', 'paprika'), ('Anime', 'Grave of the Fireflies', 'grave of the fireflies'),
    ('Anime', 'My Neighbor Totoro', 'totoro'), ('Anime', 'Howl\'s Moving Castle', 'howl'),
    ('Anime', 'Weathering with You', 'weathering with you'), ('Anime', 'A Silent Voice', 'silent voice'),
    ('Anime', 'Jujutsu Kaisen', 'jujutsu kaisen'), ('Anime', 'Chainsaw Man', 'chainsaw man'), ('Anime', 'Frieren', 'frieren'),
    # Games (26)
    ('Games', 'Castlevania', 'castlevania'), ('Games', 'Mega Man', 'mega man'), ('Games', 'Contra', 'contra'),
    ('Games', 'Donkey Kong', 'donkey kong'), ('Games', 'Kirby', 'kirby'), ('Games', 'Star Fox', 'star fox'),
    ('Games', 'F-Zero', 'f zero'), ('Games', 'EarthBound', 'earthbound'), ('Games', 'Chrono Trigger', 'chrono trigger'),
    ('Games', 'Dragon Quest', 'dragon quest'), ('Games', 'Fire Emblem', 'fire emblem'), ('Games', 'Animal Crossing', 'animal crossing'),
    ('Games', 'Splatoon', 'splatoon'), ('Games', 'Hollow Knight', 'hollow knight'), ('Games', 'Stardew Valley', 'stardew valley'),
    ('Games', 'Minecraft', 'minecraft'), ('Games', 'Terraria', 'terraria'), ('Games', 'Undertale', 'undertale'),
    ('Games', 'Hades', 'hades'), ('Games', 'Celeste', 'celeste'), ('Games', 'Cuphead', 'cuphead'),
    ('Games', 'Silent Hill', 'silent hill'), ('Games', 'Far Cry', 'far cry'), ('Games', 'Borderlands', 'borderlands'),
    ('Games', 'Red Dead Redemption', 'red dead redemption'), ('Games', 'Warcraft', 'warcraft'),
    # Light novels (14)
    ('Light novels', 'Toaru Majutsu no Index', 'index'), ('Light novels', 'Kino\'s Journey', 'kino'),
    ('Light novels', 'Boogiepop', 'boogiepop'), ('Light novels', 'Baccano!', 'baccano'),
    ('Light novels', 'Full Metal Panic!', 'full metal panic'), ('Light novels', 'Slayers', 'slayers'),
    ('Light novels', 'Record of Lodoss War', 'lodoss'), ('Light novels', 'The Irregular at Magic High School', 'irregular'),
    ('Light novels', 'Accel World', 'accel world'), ('Light novels', 'Shakugan no Shana', 'shana'),
    ('Light novels', 'Hyouka', 'hyouka'), ('Light novels', 'Rascal Does Not Dream of Bunny Girl Senpai', 'bunny girl'),
    ('Light novels', 'Torture Princess', 'torture princess'), ('Light novels', 'Bofuri', 'bofuri'),
]
EDITION = re.compile(r'\b(paperback|hardcover|hardback|kindle edition|mass market|box(ed)? set|boxset|collector\'?s|'
                     r'omnibus|deluxe edition|anniversary edition|special edition|illustrated edition|audiobook|'
                     r'unabridged|large print|\d+(st|nd|rd|th) edition|edition)\b', re.I)


def norm(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', str(value or '')) if not unicodedata.combining(c))
    return re.sub(r'[\W_]+', ' ', str(value or '').casefold().replace('&', ' and ')).strip()


def check(category, query, expect, options):
    started = time.time()
    try:
        response = resolve(query, category, options, {}, None)
    except Exception as exc:
        return {'category': category, 'query': query, 'error': f'{type(exc).__name__}: {exc}'}
    results = response.get('results') or []
    top = [{'title': r.get('title'), 'author': r.get('author'), 'kind': r.get('kind'), 'year': (r.get('release_date') or '')[:4],
            'source': r.get('display_source') or r.get('source'), 'format': r.get('format')} for r in results[:10]]
    expected_rank = next((i + 1 for i, r in enumerate(results[:20]) if norm(expect) in norm(r.get('title'))), None)
    bare = lambda text: re.sub(r'^(?:the|a|an) ', '', norm(text))
    exact_rank = next((i + 1 for i, r in enumerate(results[:20]) if bare(r.get('title')) == bare(query)), None)
    keys = {}
    for row in results[:40]:
        if row.get('kind') == 'book' and row.get('series'):
            continue  # series member rows are expected under a series bundle
        subtype = row.get('video_subtype') or row.get('game_subtype') or row.get('comic_entity') or row.get('entity_type')
        year = (row.get('release_date') or '')[:4] or str(row.get('release_year') or row.get('series_start_year') or '')
        key = (norm(row.get('title')), norm(row.get('author') if str(row.get('author')).casefold() != 'unknown' else ''),
               row.get('kind'), subtype, year)
        keys.setdefault(key, []).append(row.get('display_source') or row.get('source'))
    duplicates = [{'title': key[0], 'author': key[1], 'kind': key[2], 'type': key[3], 'year': key[4], 'sources': sources}
                  for key, sources in keys.items() if len(sources) > 1]
    editions = [r.get('title') for r in results[:40] if EDITION.search(str(r.get('title') or ''))]
    return {'category': category, 'query': query, 'seconds': round(time.time() - started, 1), 'count': len(results),
            'expected_rank': expected_rank, 'exact_rank': exact_rank, 'duplicates': duplicates, 'edition_rows': editions,
            'series_name': response.get('series_name'), 'series_members': response.get('series_member_count'),
            'errors': (response.get('errors') or [])[:6], 'top': top}


def main():
    # Titles like "Shōgun" must not crash the run on a Windows console or log file.
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    output = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else Path('build/metadata-check.json')
    only = sys.argv[sys.argv.index('--only') + 1] if '--only' in sys.argv else None
    with tempfile.TemporaryDirectory() as root:
        options = {'rights_evidence_path': str(ensure_rights_evidence(Path(root) / 'rights-evidence.json'))}
        report = []
        if '--resume' in sys.argv and output.exists():
            # Continue an interrupted run: keep finished queries, run the rest.
            report = [row for row in json.loads(output.read_text(encoding='utf-8')) if 'error' not in row]
        done = {(row['category'], row['query']) for row in report}
        chosen = (FIFTH_QUERIES if '--fifth' in sys.argv else FOURTH_QUERIES if '--fourth' in sys.argv else
                  THIRD_QUERIES if '--third' in sys.argv else MORE_QUERIES if '--more' in sys.argv else QUERIES)
        for category, query, expect in chosen:
            if (category, query) in done:
                continue
            if only and category != only:
                continue
            if category == 'Comics' and report and report[-1]['category'] == 'Comics':
                time.sleep(COMICS_PAUSE)  # the Grand Comics Database limits bursts of searches
            result = check(category, query, expect, options)
            report.append(result)
            flag = ('ERROR ' + result['error']) if 'error' in result else (
                f"rank={result['expected_rank']} exact={result['exact_rank']} n={result['count']} dup={len(result['duplicates'])} "
                f"editions={len(result['edition_rows'])} errs={len(result['errors'])} {result['seconds']}s")
            print(f'{category:12} {query:32} {flag}', flush=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
