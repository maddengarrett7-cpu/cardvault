"""
Rookie class reference data by sport and year.
Used to help Gemini accurately identify rookie cards without guessing.
Only injected for raw cards — graded cards are identified by their label.
"""

ROOKIE_CLASSES = {
    "NBA": {
        2025: [
            "Cooper Flagg", "Dylan Harper", "Ace Bailey", "VJ Edgecombe",
            "Tre Johnson", "Noa Essengue", "Kon Knueppel", "Khaman Maluach",
            "Egor Demin", "Kasparas Jakucionis", "Liam McNeeley", "Collin Murray-Boyles",
            "Jalil Bethea", "Johni Broome", "Dink Pate", "Nique Clifford",
            "Will Riley", "Rasheer Fleming", "Jeremiah Fears", "Bam Imo",
            "Hugo Gonzalez", "Walter Clayton Jr.", "Danny Wolf", "Asa Newell",
            "Jase Richardson", "Thomas Sorber", "Eric Dixon", "Labaron Philon",
            "Ian Jackson", "Kobe Bufkin",
        ],
        2024: [
            "Zaccharie Risacher", "Alex Sarr", "Reed Sheppard", "Stephon Castle",
            "Ron Holland", "Tidjane Salaün", "Donovan Clingan", "Rob Dillingham",
            "Zach Edey", "Cody Williams", "Matas Buzelis", "Nikola Topic",
            "Devin Carter", "Bub Carrington", "Kel'el Ware", "Jared McCain",
            "Dalton Knecht", "Tristan da Silva", "Ja'Kobe Walter", "Jaylon Tyson",
            "Yves Missi", "DaRon Holmes II", "AJ Johnson", "Kyshawn George",
            "Pacome Dadiet", "Dillon Jones", "Terrence Shannon Jr.", "Ryan Dunn",
            "Isaiah Collier", "Baylor Scheierman", "Jonathan Mogbo", "Kyle Filipowski",
            "Tyler Smith", "Tyler Kolek", "Johnny Furphy", "Juan Nunez",
            "Bobi Klintman", "Ajay Mitchell", "Jaylen Wells", "Oso Ighodaro",
            "Adem Bona", "KJ Simpson", "Nikola Durisic", "Pelle Larsson",
            "Jamal Shead", "Cam Christie", "Antonio Reeves", "Harrison Ingram",
            "Tristen Newton", "Enrique Freeman", "Melvin Ajinca", "Quinten Post",
            "Cam Spencer", "Anton Watson", "Bronny James", "Kevin McCullar Jr.",
            "Ulrich Chomche", "Ariel Hukporti",
        ],
        2023: [
            "Victor Wembanyama", "Brandon Miller", "Scoot Henderson", "Amen Thompson",
            "Ausar Thompson", "Anthony Black", "Bilal Coulibaly", "Jarace Walker",
            "Taylor Hendricks", "Cason Wallace", "Jett Howard", "Dereck Lively II",
            "Gradey Dick", "Jordan Hawkins", "Kobe Bufkin", "Keyonte George",
            "Jalen Hood-Schifino", "Jaime Jaquez Jr.", "Brandin Podziemski",
            "Cam Whitmore", "Noah Clowney", "Dariq Whitehead", "Kris Murray",
            "Olivier-Maxence Prosper", "Marcus Sasser", "Ben Sheppard",
            "Nick Smith Jr.", "Brice Sensabaugh", "Julian Strawther", "Kobe Brown",
            "James Nnaji", "Jalen Pickett", "Leonard Miller", "Colby Jones",
            "Julian Phillips", "Andre Jackson Jr.", "Hunter Tyson", "Jordan Walsh",
            "Mouhamed Gueye", "Maxwell Lewis", "Amari Bailey", "Tristan Vukcevic",
            "Rayan Rupert", "Sidy Cissoko", "GG Jackson", "Seth Lundy",
            "Jordan Miller", "Emoni Bates", "Keyontae Johnson", "Jalen Wilson",
            "Toumani Camara", "Jaylen Clark", "Jalen Slawson", "Isaiah Wong",
            "Tarik Biberovic", "Trayce Jackson-Davis", "Chris Livingston",
        ],
        2022: [
            "Paolo Banchero", "Chet Holmgren", "Jabari Smith Jr.", "Keegan Murray",
            "Shaedon Sharpe", "Bennedict Mathurin", "Dyson Daniels", "AJ Griffin",
            "Evan Mobley", "Jalen Duren", "Johnny Davis", "Walker Kessler",
            "Jalen Williams", "Blake Wesley", "Ochai Agbaji", "Mark Williams",
            "Nikola Jovic", "E.J. Liddell", "Tari Eason", "Andrew Nembhard",
            "Wendell Moore Jr.", "MarJon Beauchamp", "Jeremy Sochan", "Ousmane Dieng",
            "Malaki Branham", "Jake LaRavia", "Tyrese Martin", "TyTy Washington Jr.",
            "Christian Braun", "Kennedy Chandler", "Patrick Baldwin Jr.",
            "Jamaree Bouyea", "David Roddy", "Bryce McGowens", "Max Christie",
            "Darius Days", "Josh Minott", "Gui Santos", "Trevor Keels",
        ],
    },
    "NFL": {
        2025: [
            "Cam Ward", "Travis Hunter", "Abdul Carter", "Will Campbell",
            "Ashton Jeanty", "Tetairoa McMillan", "Mason Graham", "Kelvin Banks Jr.",
            "Malaki Starks", "Luther Burden III", "Jalon Walker", "Jihaad Campbell",
            "Walter Nolen", "Mykel Williams", "James Pearce Jr.", "Shedeur Sanders",
            "Darius Alexander", "Grey Zabel", "Omarion Hampton", "Emeka Egbuka",
            "Tyler Warren", "Shemar Stewart", "Josh Simmons", "Tyleik Williams",
            "Matthew Golden", "Armand Membou", "Josaiah Stewart", "Isaiah Campbell",
            "Quinshon Judkins", "Nick Emmanwori", "Derrick Harmon", "Donovan Ezeiruaku",
            "Kyle Williams", "Harold Perkins Jr.", "RJ Harvey", "Jack Sawyer",
            "Princely Umanmielen", "Aireontae Ersery", "TreVeyon Henderson",
            "Colston Loveland", "Elijah Arroyo", "Drew Allar", "Dillon Gabriel",
        ],
        2024: [
            "Caleb Williams", "Jayden Daniels", "Drake Maye", "Marvin Harrison Jr.",
            "Joe Alt", "Malik Nabers", "JC Latham", "Michael Penix Jr.",
            "Rome Odunze", "JJ McCarthy", "Olu Fashanu", "Bo Nix",
            "Brock Bowers", "Taliese Fuaga", "Laiatu Latu", "Byron Murphy II",
            "Dallas Turner", "Amarius Mims", "Jared Verse", "Troy Fautanu",
            "Chop Robinson", "Quinyon Mitchell", "Brian Thomas Jr.", "Terrion Arnold",
            "Jordan Morgan", "Graham Barton", "Darius Robinson", "Xavier Worthy",
            "Tyler Guyton", "Nate Wiggins", "Ricky Pearsall", "Xavier Legette",
            "Keon Coleman", "Ladd McConkey", "Ruke Orhorhoro", "Jer'Zhan Newton",
            "Ja'Lynn Polk", "T'Vondre Sweat", "Braden Fiske", "Cooper DeJean",
            "Kool-Aid McKinstry", "Kamari Lassiter", "Max Melton",
            "Jackson Powers-Johnson", "Edgerrin Cooper", "Jonathon Brooks",
            "Tyler Nubin", "Maason Smith", "Kris Jenkins", "Mike Sainristil",
            "Zach Frazier", "Adonai Mitchell", "Ben Sinnott", "Mike Hall Jr.",
            "Patrick Paul", "Marshawn Kneeland", "Chris Braswell", "Javon Bullard",
            "Blake Fisher", "Cole Bishop", "Ennis Rakestraw Jr.", "Roger Rosengarten",
            "Kingsley Suamataia", "Renardo Green", "Bucky Irving", "Tyrone Tracy Jr.",
            "Roman Wilson", "Trey Benson", "Ray Davis", "MarShawn Lloyd",
            "Kimani Vidal", "Johnny Newton", "Braden Fiske", "Darius Robinson",
        ],
        2023: [
            "Bryce Young", "CJ Stroud", "Will Anderson Jr.", "Anthony Richardson",
            "Devon Witherspoon", "Paris Johnson Jr.", "Tyree Wilson", "Bijan Robinson",
            "Jalen Carter", "Darnell Wright", "Peter Skoronski", "Jahmyr Gibbs",
            "Lukas Van Ness", "Broderick Jones", "Will McDonald IV", "Emmanuel Forbes",
            "Christian Gonzalez", "Jack Campbell", "Calijah Kancey",
            "Jaxon Smith-Njigba", "Quentin Johnston", "Zay Flowers", "Jordan Addison",
            "Deonte Banks", "Dalton Kincaid", "Mazi Smith", "Anton Harrison",
            "Myles Murphy", "Bryan Bresee", "Nolan Smith", "Felix Anudike-Uzomah",
            "Joey Porter Jr.", "Will Levis", "Sam LaPorta", "Michael Mayer",
            "Steve Avila", "Derick Hall", "Matthew Bergeron", "Jonathan Mingo",
            "Isaiah Foskey", "BJ Ojulari", "Luke Musgrave", "Joe Tippmann",
            "JuJu Brents", "Brian Branch", "Keion White", "Quan Martin",
            "Cody Mauch", "Keeanu Benton", "Jayden Reed", "Cam Smith",
            "Zach Charbonnet", "Gervon Dexter", "Tuli Tuipulotu", "Rashee Rice",
            "Tyrique Stevenson", "John Michael Schmitz", "Luke Schoonmaker",
            "OShyheim Torrence", "DJ Turner", "Brenton Strange", "Juice Scruggs",
            "Marvin Mims",
        ],
        2022: [
            "Travon Walker", "Aidan Hutchinson", "Derek Stingley Jr.", "Ahmad Gardner",
            "Kayvon Thibodeaux", "Evan Neal", "Trent McDuffie", "Drake London",
            "Garrett Wilson", "Ikem Ekwonu", "Trevor Penning", "Kyle Hamilton",
            "Jameson Williams", "Chris Olave", "Jermaine Johnson II", "Quay Walker",
            "Devonte Wyatt", "Kenyon Green", "Tyler Linderbaum", "George Karlaftis",
            "Breece Hall", "Jeremy Ruckert", "Skyy Moore", "Christian Watson",
            "Treylon Burks", "Bernhard Raimann", "Desmond Ridder", "Matt Corral",
            "Sam Howell", "Kenny Pickett", "Malik Willis", "Bailey Zappe",
            "Wan'Dale Robinson", "David Bell", "John Metchie III", "Alec Pierce",
            "Romeo Doubs", "Rachaad White", "Brian Robinson Jr.", "Dameon Pierce",
        ],
    },
    "MLB": {
        2025: [
            "Roki Sasaki", "Jurrangelo Cijntje", "Rhett Lowder", "Jac Caglianone",
            "Chase Burns", "Kyson Donahue", "Charlie Condon", "Bryce Eldridge",
            "Travis Bazzana", "Wyatt Langford", "Jackson Chourio", "Jackson Merrill",
        ],
        2024: [
            "Jackson Chourio", "Wyatt Langford", "Paul Skenes", "Jackson Merrill",
            "Yoshinobu Yamamoto", "Spencer Jones", "Dylan Crews", "Colt Keith",
            "Junior Caminero", "Evan Carter", "Kyle Harrison", "Hurston Waldrep",
            "Grant Holmes", "Davis Schneider", "Masyn Winn", "Tyler Black",
            "Spencer Arrighetti", "Luis Gil", "Bryce Miller", "Tanner Bibee",
        ],
        2023: [
            "Corbin Carroll", "Gunnar Henderson", "Ezequiel Tovar", "Julio Rodriguez",
            "Bobby Miller", "Jordan Walker", "Eury Perez", "Anthony Volpe",
            "Michael Harris II", "Spencer Strider", "Triston Casas", "Josh Jung",
            "Brett Baty", "Grayson Rodriguez", "Joey Wiemer", "Oscar Colas",
            "Kyle Manzardo", "Bryan De La Cruz", "Zac Veen", "Jordan Lawlar",
        ],
    },
    "NHL": {
        2024: [
            "Macklin Celebrini", "Matvei Michkov", "Beckett Sennecke", "Cayden Lindstrom",
            "Cole Eiserman", "Artyom Levshunov", "Anton Silayev", "Sam Dickinson",
            "Zayne Parekh", "Michael Misa", "Porter Martone", "Tij Iginla",
            "Oscar Fisker Nilsson", "Blake Fiddler", "Konsta Helenius",
        ],
        2023: [
            "Connor Bedard", "Adam Fantilli", "Will Smith", "Ryan Leonard",
            "Matvei Michkov", "Leo Carlsson", "Brayden Yager", "Zach Benson",
            "Dalibor Dvorsky", "Oliver Moore", "David Reinbacher", "Colby Barlow",
            "Matthew Wood", "Noel Nordh", "Riley Heidt",
        ],
    },
}


def get_rookie_hint(year, sport=None):
    """
    Return a rookie name hint string for injection into Gemini prompts.
    Returns None if no data available for that year/sport.
    Only call this for raw cards.
    """
    if not year:
        return None

    try:
        year = int(year)
    except (ValueError, TypeError):
        return None

    hints = []

    sports_to_check = []
    if sport:
        s = sport.strip().upper()
        if "BASKET" in s or s == "NBA":
            sports_to_check = ["NBA"]
        elif "FOOT" in s or s == "NFL":
            sports_to_check = ["NFL"]
        elif "BASE" in s or s == "MLB":
            sports_to_check = ["MLB"]
        elif "HOCKEY" in s or s == "NHL":
            sports_to_check = ["NHL"]
        else:
            sports_to_check = list(ROOKIE_CLASSES.keys())
    else:
        sports_to_check = list(ROOKIE_CLASSES.keys())

    for sp in sports_to_check:
        names = ROOKIE_CLASSES.get(sp, {}).get(year)
        if names:
            hints.append(f"{sp} {year} Rookie Class: {', '.join(names)}")

    return "\n".join(hints) if hints else None
