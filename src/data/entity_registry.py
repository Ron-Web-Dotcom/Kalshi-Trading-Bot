"""
Comprehensive entity registry for prediction market live-event detection.
All names are lowercase strings for fast substring matching against API data.
"""

from typing import Dict, Set

# Each category maps a canonical name → set of aliases/abbreviations (all lowercase)
# The bot checks if ANY alias appears in live event data from SofaScore/ESPN/news

SPORTS: Dict[str, Set[str]] = {

    # -------------------------------------------------------------------------
    # NFL - All 32 Teams
    # -------------------------------------------------------------------------
    "arizona cardinals": {"cardinals", "arizona cardinals", "arizona", "ari cardinals", "ari"},
    "atlanta falcons": {"falcons", "atlanta falcons", "atlanta", "atl falcons", "atl"},
    "baltimore ravens": {"ravens", "baltimore ravens", "baltimore", "bal ravens", "bal"},
    "buffalo bills": {"bills", "buffalo bills", "buffalo", "buf bills", "buf"},
    "carolina panthers": {"panthers", "carolina panthers", "carolina", "car panthers", "car"},
    "chicago bears": {"bears", "chicago bears", "chicago", "chi bears", "chi"},
    "cincinnati bengals": {"bengals", "cincinnati bengals", "cincinnati", "cin bengals", "cin"},
    "cleveland browns": {"browns", "cleveland browns", "cleveland", "cle browns", "cle"},
    "dallas cowboys": {"cowboys", "dallas cowboys", "dallas", "dal cowboys", "dal", "america's team"},
    "denver broncos": {"broncos", "denver broncos", "denver", "den broncos", "den"},
    "detroit lions": {"lions", "detroit lions", "detroit", "det lions", "det"},
    "green bay packers": {"packers", "green bay packers", "green bay", "gb packers", "gb"},
    "houston texans": {"texans", "houston texans", "houston", "hou texans", "hou"},
    "indianapolis colts": {"colts", "indianapolis colts", "indianapolis", "ind colts", "indy"},
    "jacksonville jaguars": {"jaguars", "jacksonville jaguars", "jacksonville", "jax jaguars", "jax"},
    "kansas city chiefs": {"chiefs", "kansas city chiefs", "kansas city", "kc chiefs", "kc"},
    "las vegas raiders": {"raiders", "las vegas raiders", "las vegas", "lv raiders", "oakland raiders"},
    "los angeles chargers": {"chargers", "los angeles chargers", "la chargers", "lac"},
    "los angeles rams": {"rams", "los angeles rams", "la rams", "lar"},
    "miami dolphins": {"dolphins", "miami dolphins", "miami", "mia dolphins", "mia"},
    "minnesota vikings": {"vikings", "minnesota vikings", "minnesota", "min vikings", "min"},
    "new england patriots": {"patriots", "new england patriots", "new england", "pats", "ne patriots", "ne"},
    "new orleans saints": {"saints", "new orleans saints", "new orleans", "no saints", "nor"},
    "new york giants": {"giants", "new york giants", "ny giants", "nyg"},
    "new york jets": {"jets", "new york jets", "ny jets", "nyj"},
    "philadelphia eagles": {"eagles", "philadelphia eagles", "philadelphia", "phi eagles", "phi"},
    "pittsburgh steelers": {"steelers", "pittsburgh steelers", "pittsburgh", "pit steelers", "pit"},
    "san francisco 49ers": {"49ers", "san francisco 49ers", "san francisco", "sf 49ers", "sf", "niners", "forty niners"},
    "seattle seahawks": {"seahawks", "seattle seahawks", "seattle", "sea seahawks", "sea"},
    "tampa bay buccaneers": {"buccaneers", "tampa bay buccaneers", "tampa bay", "tb buccaneers", "tb", "bucs"},
    "tennessee titans": {"titans", "tennessee titans", "tennessee", "ten titans", "ten"},
    "washington commanders": {"commanders", "washington commanders", "washington", "was commanders", "was", "washington football team", "redskins"},

    # -------------------------------------------------------------------------
    # NBA - All 30 Teams
    # -------------------------------------------------------------------------
    "atlanta hawks": {"hawks", "atlanta hawks", "atl hawks"},
    "boston celtics": {"celtics", "boston celtics", "boston", "bos celtics", "bos"},
    "brooklyn nets": {"nets", "brooklyn nets", "brooklyn", "bkn nets", "bkn"},
    "charlotte hornets": {"hornets", "charlotte hornets", "charlotte", "cha hornets", "cha"},
    "chicago bulls": {"bulls", "chicago bulls", "chi bulls"},
    "cleveland cavaliers": {"cavaliers", "cleveland cavaliers", "cavs", "cle cavaliers"},
    "dallas mavericks": {"mavericks", "dallas mavericks", "mavs", "dal mavericks"},
    "denver nuggets": {"nuggets", "denver nuggets", "den nuggets"},
    "detroit pistons": {"pistons", "detroit pistons", "det pistons"},
    "golden state warriors": {"warriors", "golden state warriors", "golden state", "gsw", "gs warriors"},
    "houston rockets": {"rockets", "houston rockets", "hou rockets"},
    "indiana pacers": {"pacers", "indiana pacers", "ind pacers"},
    "los angeles clippers": {"clippers", "los angeles clippers", "la clippers", "lac clippers"},
    "los angeles lakers": {"lakers", "los angeles lakers", "la lakers", "lal"},
    "memphis grizzlies": {"grizzlies", "memphis grizzlies", "mem grizzlies", "mem"},
    "miami heat": {"heat", "miami heat", "mia heat"},
    "milwaukee bucks": {"bucks", "milwaukee bucks", "mil bucks", "mil"},
    "minnesota timberwolves": {"timberwolves", "minnesota timberwolves", "twolves", "min timberwolves"},
    "new orleans pelicans": {"pelicans", "new orleans pelicans", "no pelicans", "nor pelicans"},
    "new york knicks": {"knicks", "new york knicks", "ny knicks", "nyk"},
    "oklahoma city thunder": {"thunder", "oklahoma city thunder", "oklahoma city", "okc thunder", "okc"},
    "orlando magic": {"magic", "orlando magic", "orl magic", "orl"},
    "philadelphia 76ers": {"76ers", "philadelphia 76ers", "sixers", "phi 76ers"},
    "phoenix suns": {"suns", "phoenix suns", "phx suns", "phx"},
    "portland trail blazers": {"trail blazers", "portland trail blazers", "portland", "blazers", "por"},
    "sacramento kings": {"kings", "sacramento kings", "sacramento", "sac kings", "sac"},
    "san antonio spurs": {"spurs", "san antonio spurs", "san antonio", "sa spurs", "sas"},
    "toronto raptors": {"raptors", "toronto raptors", "toronto", "tor raptors", "tor"},
    "utah jazz": {"jazz", "utah jazz", "utah", "uta jazz", "uta"},
    "washington wizards": {"wizards", "washington wizards", "was wizards", "wiz"},

    # -------------------------------------------------------------------------
    # MLB - All 30 Teams
    # -------------------------------------------------------------------------
    "arizona diamondbacks": {"diamondbacks", "arizona diamondbacks", "dbacks", "ari diamondbacks"},
    "atlanta braves": {"braves", "atlanta braves", "atl braves"},
    "baltimore orioles": {"orioles", "baltimore orioles", "bal orioles", "o's"},
    "boston red sox": {"red sox", "boston red sox", "bos red sox", "redsox"},
    "chicago cubs": {"cubs", "chicago cubs", "chi cubs"},
    "chicago white sox": {"white sox", "chicago white sox", "chw", "chi white sox"},
    "cincinnati reds": {"reds", "cincinnati reds", "cin reds"},
    "cleveland guardians": {"guardians", "cleveland guardians", "cle guardians", "cleveland indians"},
    "colorado rockies": {"rockies", "colorado rockies", "col rockies", "col"},
    "detroit tigers": {"tigers", "detroit tigers", "det tigers"},
    "houston astros": {"astros", "houston astros", "hou astros"},
    "kansas city royals": {"royals", "kansas city royals", "kc royals"},
    "los angeles angels": {"angels", "los angeles angels", "la angels", "laa", "anaheim angels"},
    "los angeles dodgers": {"dodgers", "los angeles dodgers", "la dodgers", "lad"},
    "miami marlins": {"marlins", "miami marlins", "mia marlins"},
    "milwaukee brewers": {"brewers", "milwaukee brewers", "mil brewers"},
    "minnesota twins": {"twins", "minnesota twins", "min twins"},
    "new york mets": {"mets", "new york mets", "ny mets", "nym"},
    "new york yankees": {"yankees", "new york yankees", "ny yankees", "nyy", "bronx bombers"},
    "oakland athletics": {"athletics", "oakland athletics", "oakland a's", "oak", "a's", "as"},
    "philadelphia phillies": {"phillies", "philadelphia phillies", "phi phillies"},
    "pittsburgh pirates": {"pirates", "pittsburgh pirates", "pit pirates"},
    "san diego padres": {"padres", "san diego padres", "sd padres", "sdp"},
    "san francisco giants": {"sf giants", "san francisco giants", "sfg"},
    "seattle mariners": {"mariners", "seattle mariners", "sea mariners"},
    "st. louis cardinals": {"st louis cardinals", "stl cardinals", "stl", "cardinals"},
    "tampa bay rays": {"rays", "tampa bay rays", "tb rays"},
    "texas rangers": {"rangers", "texas rangers", "tex rangers", "tex"},
    "toronto blue jays": {"blue jays", "toronto blue jays", "tor blue jays", "bluejays"},
    "washington nationals": {"nationals", "washington nationals", "was nationals", "nats"},

    # -------------------------------------------------------------------------
    # NHL - All 32 Teams
    # -------------------------------------------------------------------------
    "anaheim ducks": {"ducks", "anaheim ducks", "ana ducks", "ana"},
    "arizona coyotes": {"coyotes", "arizona coyotes", "ari coyotes", "utah hockey club"},
    "boston bruins": {"bruins", "boston bruins", "bos bruins"},
    "buffalo sabres": {"sabres", "buffalo sabres", "buf sabres"},
    "calgary flames": {"flames", "calgary flames", "cgy flames", "cgy"},
    "carolina hurricanes": {"hurricanes", "carolina hurricanes", "car hurricanes"},
    "chicago blackhawks": {"blackhawks", "chicago blackhawks", "chi blackhawks"},
    "colorado avalanche": {"avalanche", "colorado avalanche", "col avalanche", "avs"},
    "columbus blue jackets": {"blue jackets", "columbus blue jackets", "cbj"},
    "dallas stars": {"stars", "dallas stars", "dal stars"},
    "detroit red wings": {"red wings", "detroit red wings", "det red wings"},
    "edmonton oilers": {"oilers", "edmonton oilers", "edm oilers", "edm"},
    "florida panthers": {"florida panthers", "fla panthers", "fla"},
    "los angeles kings": {"kings", "los angeles kings", "la kings", "lak"},
    "minnesota wild": {"wild", "minnesota wild", "min wild"},
    "montreal canadiens": {"canadiens", "montreal canadiens", "mtl canadiens", "mtl", "habs"},
    "nashville predators": {"predators", "nashville predators", "nsh predators", "nsh", "preds"},
    "new jersey devils": {"devils", "new jersey devils", "nj devils", "njd"},
    "new york islanders": {"islanders", "new york islanders", "ny islanders", "nyi"},
    "new york rangers": {"ny rangers", "new york rangers", "nyr"},
    "ottawa senators": {"senators", "ottawa senators", "ott senators", "ott"},
    "philadelphia flyers": {"flyers", "philadelphia flyers", "phi flyers"},
    "pittsburgh penguins": {"penguins", "pittsburgh penguins", "pit penguins", "pens"},
    "san jose sharks": {"sharks", "san jose sharks", "sjs"},
    "seattle kraken": {"kraken", "seattle kraken", "sea kraken"},
    "st. louis blues": {"blues", "st louis blues", "stl blues"},
    "tampa bay lightning": {"lightning", "tampa bay lightning", "tb lightning", "bolts"},
    "toronto maple leafs": {"maple leafs", "toronto maple leafs", "tor maple leafs", "leafs"},
    "vancouver canucks": {"canucks", "vancouver canucks", "van canucks", "van"},
    "vegas golden knights": {"golden knights", "vegas golden knights", "vgk", "vegas knights"},
    "washington capitals": {"capitals", "washington capitals", "was capitals", "caps"},
    "winnipeg jets": {"winnipeg jets", "wpg jets", "wpg"},

    # -------------------------------------------------------------------------
    # MLS - All Teams
    # -------------------------------------------------------------------------
    "atlanta united": {"atlanta united", "atl united", "atl utd"},
    "austin fc": {"austin fc", "atx fc"},
    "charlotte fc": {"charlotte fc", "clt fc"},
    "chicago fire": {"chicago fire", "chi fire"},
    "fc cincinnati": {"fc cincinnati", "cin fc", "cincinnati fc"},
    "colorado rapids": {"colorado rapids", "col rapids"},
    "columbus crew": {"columbus crew", "clb crew"},
    "d.c. united": {"dc united", "d.c. united", "dcu"},
    "fc dallas": {"fc dallas", "dal fc", "dallas fc"},
    "houston dynamo": {"houston dynamo", "hou dynamo"},
    "inter miami": {"inter miami", "mia inter", "miami cf"},
    "la galaxy": {"la galaxy", "los angeles galaxy", "lag"},
    "lafc": {"lafc", "los angeles fc", "la fc"},
    "minnesota united": {"minnesota united", "min united", "loons"},
    "cf montreal": {"cf montreal", "montreal impact", "mtl cf"},
    "nashville sc": {"nashville sc", "nsh sc"},
    "new england revolution": {"new england revolution", "ne revolution", "revs"},
    "new york city fc": {"new york city fc", "nycfc", "nyc fc"},
    "new york red bulls": {"new york red bulls", "ny red bulls", "nyrb"},
    "orlando city": {"orlando city", "orl city"},
    "philadelphia union": {"philadelphia union", "phi union"},
    "portland timbers": {"portland timbers", "por timbers"},
    "real salt lake": {"real salt lake", "rsl"},
    "san jose earthquakes": {"san jose earthquakes", "sj earthquakes", "quakes"},
    "seattle sounders": {"seattle sounders", "sea sounders"},
    "sporting kansas city": {"sporting kansas city", "skc", "sporting kc"},
    "toronto fc": {"toronto fc", "tor fc"},
    "vancouver whitecaps": {"vancouver whitecaps", "van whitecaps"},
    "st. louis city sc": {"st louis city sc", "stl city sc"},
    "san diego fc": {"san diego fc", "sd fc"},
    "utah royals": {"utah royals", "uta royals"},

    # -------------------------------------------------------------------------
    # EPL - English Premier League
    # -------------------------------------------------------------------------
    "arsenal": {"arsenal", "the gunners", "gunners", "ars"},
    "aston villa": {"aston villa", "villa", "avl"},
    "bournemouth": {"bournemouth", "afc bournemouth", "bou"},
    "brentford": {"brentford", "bre"},
    "brighton": {"brighton", "brighton hove albion", "brighton & hove albion", "bha"},
    "burnley": {"burnley", "bur"},
    "chelsea": {"chelsea", "the blues", "che"},
    "crystal palace": {"crystal palace", "palace", "cpfc", "cpa"},
    "everton": {"everton", "the toffees", "toffees", "eve"},
    "fulham": {"fulham", "ful"},
    "ipswich town": {"ipswich town", "ipswich", "ips"},
    "leicester city": {"leicester city", "leicester", "the foxes", "lei"},
    "liverpool": {"liverpool", "the reds", "lfc", "liv"},
    "luton town": {"luton town", "luton", "lut"},
    "manchester city": {"manchester city", "man city", "man c", "mci", "city"},
    "manchester united": {"manchester united", "man united", "man utd", "man u", "manu", "mun"},
    "newcastle united": {"newcastle united", "newcastle", "the magpies", "newcastle utd", "new"},
    "nottingham forest": {"nottingham forest", "notts forest", "nfo", "forest"},
    "sheffield united": {"sheffield united", "sheffield utd", "shu"},
    "southampton": {"southampton", "saints", "sou"},
    "tottenham hotspur": {"tottenham hotspur", "tottenham", "spurs", "thfc", "tot"},
    "watford": {"watford", "wat"},
    "west ham united": {"west ham united", "west ham", "whu", "hammers"},
    "wolverhampton": {"wolverhampton", "wolves", "wol"},

    # -------------------------------------------------------------------------
    # La Liga - Spanish
    # -------------------------------------------------------------------------
    "real madrid": {"real madrid", "madrid", "los blancos", "rma"},
    "barcelona": {"barcelona", "fc barcelona", "fcb", "barca", "bar"},
    "atletico madrid": {"atletico madrid", "atletico", "atleti", "atm"},
    "sevilla": {"sevilla", "sev"},
    "real sociedad": {"real sociedad", "sociedad", "rso"},
    "real betis": {"real betis", "betis", "bet"},
    "villarreal": {"villarreal", "vil"},
    "athletic bilbao": {"athletic bilbao", "athletic club", "ath"},
    "valencia": {"valencia", "val"},
    "osasuna": {"osasuna", "osa"},
    "celta vigo": {"celta vigo", "celta", "cel"},
    "rayo vallecano": {"rayo vallecano", "rayo", "ray"},
    "getafe": {"getafe", "get"},
    "almeria": {"almeria", "alm"},
    "cadiz": {"cadiz", "cad"},
    "girona": {"girona", "gir"},
    "mallorca": {"mallorca", "mal"},
    "deportivo alaves": {"deportivo alaves", "alaves", "ala"},
    "espanyol": {"espanyol", "esp"},
    "levante": {"levante", "lev"},
    "granada": {"granada", "gra"},
    "leganes": {"leganes", "leg"},

    # -------------------------------------------------------------------------
    # Bundesliga - German
    # -------------------------------------------------------------------------
    "bayern munich": {"bayern munich", "fc bayern", "bayern", "fcb", "bayer münchen", "fcbayern"},
    "borussia dortmund": {"borussia dortmund", "dortmund", "bvb", "bor dortmund"},
    "bayer leverkusen": {"bayer leverkusen", "leverkusen", "b04"},
    "rb leipzig": {"rb leipzig", "leipzig", "rbl"},
    "borussia monchengladbach": {"borussia monchengladbach", "monchengladbach", "gladbach", "bmg"},
    "eintracht frankfurt": {"eintracht frankfurt", "frankfurt", "sgf"},
    "wolfsburg": {"wolfsburg", "vfl wolfsburg", "wob"},
    "sc freiburg": {"sc freiburg", "freiburg", "scf"},
    "hoffenheim": {"hoffenheim", "tsg hoffenheim", "tsg"},
    "werder bremen": {"werder bremen", "bremen", "svw"},
    "union berlin": {"union berlin", "fcu"},
    "mainz": {"mainz", "fsv mainz", "m05"},
    "augsburg": {"augsburg", "fca"},
    "hertha berlin": {"hertha berlin", "hertha", "bsc"},
    "bochum": {"bochum", "vfl bochum"},
    "stuttgarter": {"vfb stuttgart", "stuttgart"},

    # -------------------------------------------------------------------------
    # Serie A - Italian
    # -------------------------------------------------------------------------
    "juventus": {"juventus", "juve", "jvs"},
    "inter milan": {"inter milan", "inter", "internazionale", "nerazzurri"},
    "ac milan": {"ac milan", "milan", "rossoneri"},
    "napoli": {"napoli", "ssc napoli"},
    "as roma": {"as roma", "roma", "giallorossi"},
    "lazio": {"lazio", "ss lazio"},
    "atalanta": {"atalanta", "dea"},
    "fiorentina": {"fiorentina", "viola", "acf fiorentina"},
    "torino": {"torino", "granata"},
    "bologna": {"bologna", "bfc"},
    "udinese": {"udinese"},
    "sassuolo": {"sassuolo"},
    "empoli": {"empoli"},
    "verona": {"hellas verona", "verona"},
    "lecce": {"lecce", "us lecce"},
    "monza": {"monza", "ac monza"},
    "cagliari": {"cagliari"},
    "genoa": {"genoa", "cfc"},
    "frosinone": {"frosinone"},
    "salernitana": {"salernitana", "us salernitana"},
    "venezia": {"venezia", "fc venezia"},
    "como": {"como 1907", "como"},

    # -------------------------------------------------------------------------
    # Ligue 1 - French
    # -------------------------------------------------------------------------
    "paris saint-germain": {"paris saint-germain", "psg", "paris sg", "paris"},
    "marseille": {"marseille", "olympique marseille", "om"},
    "lyon": {"lyon", "olympique lyonnais", "ol"},
    "monaco": {"monaco", "as monaco"},
    "lille": {"lille", "losc lille", "losc"},
    "nice": {"nice", "ogc nice"},
    "rennes": {"rennes", "stade rennais"},
    "lens": {"lens", "rc lens"},
    "strasbourg": {"strasbourg", "rc strasbourg"},
    "montpellier": {"montpellier", "mhsc"},
    "nantes": {"nantes", "fc nantes"},
    "toulouse": {"toulouse", "tfc"},
    "reims": {"reims", "stade reims"},
    "lorient": {"lorient", "fc lorient"},
    "brest": {"brest", "stade brestois"},
    "metz": {"metz", "fc metz"},
    "le havre": {"le havre", "lehavre"},
    "clermont": {"clermont foot", "clermont"},
    "angers": {"angers", "sco angers"},
    "auxerre": {"auxerre", "aja"},

    # -------------------------------------------------------------------------
    # UEFA Champions League / Europa League Notable Clubs
    # -------------------------------------------------------------------------
    "porto": {"porto", "fc porto"},
    "benfica": {"benfica", "sl benfica"},
    "sporting cp": {"sporting cp", "sporting lisbon", "sporting"},
    "psv eindhoven": {"psv eindhoven", "psv"},
    "ajax": {"ajax", "afc ajax"},
    "feyenoord": {"feyenoord"},
    "celtic": {"celtic", "celtic fc"},
    "rangers": {"rangers fc", "glasgow rangers"},
    "shakhtar donetsk": {"shakhtar donetsk", "shakhtar"},
    "dynamo kyiv": {"dynamo kyiv", "dynamo kiev"},
    "galatasaray": {"galatasaray", "gala"},
    "fenerbahce": {"fenerbahce"},
    "besiktas": {"besiktas"},
    "red bull salzburg": {"red bull salzburg", "salzburg", "rbs"},
    "young boys": {"young boys", "bsc young boys"},
    "anderlecht": {"anderlecht", "rsc anderlecht"},
    "club brugge": {"club brugge", "brugge"},
    "olympiacos": {"olympiacos"},
    "panathinaikos": {"panathinaikos"},
    "crvena zvezda": {"red star belgrade", "crvena zvezda", "red star"},

    # -------------------------------------------------------------------------
    # International Football / FIFA Nations
    # -------------------------------------------------------------------------
    "brazil national football": {"brazil", "brasil", "seleção", "selecao", "cbf"},
    "argentina national football": {"argentina", "albiceleste", "afa"},
    "france national football": {"france", "les bleus", "fff"},
    "england national football": {"england", "three lions", "fa"},
    "germany national football": {"germany", "deutschland", "die mannschaft", "dfb"},
    "spain national football": {"spain", "la roja", "la furia roja", "rfef"},
    "italy national football": {"italy", "italia", "azzurri", "figc"},
    "netherlands national football": {"netherlands", "holland", "dutch", "oranje", "knvb"},
    "portugal national football": {"portugal", "seleção portuguesa", "fpf"},
    "belgium national football": {"belgium", "red devils", "rbfa"},
    "croatia national football": {"croatia", "vatreni", "hns"},
    "denmark national football": {"denmark", "danish", "dbu"},
    "switzerland national football": {"switzerland", "swiss", "sfv"},
    "uruguay national football": {"uruguay", "celeste", "auf"},
    "colombia national football": {"colombia", "los cafeteros", "fcf"},
    "mexico national football": {"mexico", "el tri", "tricolor", "fmf"},
    "united states national football": {"usa", "usmnt", "us soccer", "uswnt", "usnmt"},
    "japan national football": {"japan", "samurai blue", "jfa"},
    "south korea national football": {"south korea", "korea republic", "kor", "taegeuk warriors"},
    "australia national football": {"australia", "socceroos", "ffa"},
    "morocco national football": {"morocco", "atlas lions", "frmf"},
    "senegal national football": {"senegal", "lions of teranga", "fsf"},
    "nigeria national football": {"nigeria", "super eagles", "nff"},
    "ghana national football": {"ghana", "black stars", "gfa"},
    "ivory coast national football": {"ivory coast", "cote d'ivoire", "elephants", "fif"},
    "egypt national football": {"egypt", "pharaohs", "efa"},
    "cameroon national football": {"cameroon", "indomitable lions", "fecafoot"},
    "saudi arabia national football": {"saudi arabia", "green falcons", "saff"},
    "iran national football": {"iran", "team melli", "ffiri"},
    "qatar national football": {"qatar", "maroons", "qfa"},
    "canada national football": {"canada", "canadian men national", "csa"},
    "chile national football": {"chile", "la roja chilena", "ffch"},
    "ecuador national football": {"ecuador", "la tri", "fef"},
    "peru national football": {"peru", "blanquirroja", "fpf"},
    "paraguay national football": {"paraguay", "guarani", "apf"},
    "venezuela national football": {"venezuela", "vinotinto", "fvf"},

    # -------------------------------------------------------------------------
    # Tennis - Grand Slam & Top Players
    # -------------------------------------------------------------------------
    "novak djokovic": {"djokovic", "novak djokovic", "nole"},
    "carlos alcaraz": {"alcaraz", "carlos alcaraz"},
    "jannik sinner": {"sinner", "jannik sinner"},
    "daniil medvedev": {"medvedev", "daniil medvedev"},
    "andrey rublev": {"rublev", "andrey rublev"},
    "alexander zverev": {"zverev", "alexander zverev", "sascha"},
    "stefanos tsitsipas": {"tsitsipas", "stefanos tsitsipas"},
    "casper ruud": {"ruud", "casper ruud"},
    "holger rune": {"rune", "holger rune"},
    "taylor fritz": {"fritz", "taylor fritz"},
    "frances tiafoe": {"tiafoe", "frances tiafoe"},
    "ben shelton": {"shelton", "ben shelton"},
    "tommy paul": {"tommy paul"},
    "rafael nadal": {"nadal", "rafael nadal", "rafa", "rafa nadal"},
    "roger federer": {"federer", "roger federer"},
    "andy murray": {"murray", "andy murray"},
    "iga swiatek": {"swiatek", "iga swiatek"},
    "aryna sabalenka": {"sabalenka", "aryna sabalenka"},
    "coco gauff": {"gauff", "coco gauff"},
    "elena rybakina": {"rybakina", "elena rybakina"},
    "jessica pegula": {"pegula", "jessica pegula"},
    "barbora krejcikova": {"krejcikova", "barbora krejcikova"},
    "qinwen zheng": {"zheng", "qinwen zheng"},
    "caroline wozniacki": {"wozniacki", "caroline wozniacki"},
    "maria sakkari": {"sakkari", "maria sakkari"},
    "emma raducanu": {"raducanu", "emma raducanu"},
    "wimbledon": {"wimbledon", "the championships", "all england"},
    "us open tennis": {"us open", "flushing meadows", "usta"},
    "french open": {"french open", "roland garros", "roland-garros"},
    "australian open": {"australian open", "ao"},

    # -------------------------------------------------------------------------
    # Golf - Major Players
    # -------------------------------------------------------------------------
    "tiger woods": {"tiger woods", "tiger"},
    "rory mcilroy": {"mcilroy", "rory mcilroy", "rory"},
    "jon rahm": {"rahm", "jon rahm"},
    "scottie scheffler": {"scheffler", "scottie scheffler"},
    "brooks koepka": {"koepka", "brooks koepka"},
    "dustin johnson": {"dustin johnson", "dj golf"},
    "jordan spieth": {"spieth", "jordan spieth"},
    "justin thomas": {"justin thomas", "jt golf"},
    "patrick cantlay": {"cantlay", "patrick cantlay"},
    "xander schauffele": {"schauffele", "xander schauffele"},
    "collin morikawa": {"morikawa", "collin morikawa"},
    "viktor hovland": {"hovland", "viktor hovland"},
    "will zalatoris": {"zalatoris", "will zalatoris"},
    "tony finau": {"finau", "tony finau"},
    "phil mickelson": {"mickelson", "phil mickelson", "lefty"},
    "the masters": {"masters", "the masters", "augusta national"},
    "the open championship": {"open championship", "british open", "the open"},
    "pga championship": {"pga championship"},
    "us open golf": {"us open golf"},
    "liv golf": {"liv golf", "liv"},
    "pga tour": {"pga tour"},

    # -------------------------------------------------------------------------
    # Formula 1
    # -------------------------------------------------------------------------
    "max verstappen": {"verstappen", "max verstappen", "mad max"},
    "lewis hamilton": {"hamilton", "lewis hamilton"},
    "charles leclerc": {"leclerc", "charles leclerc"},
    "lando norris": {"norris", "lando norris"},
    "carlos sainz": {"sainz", "carlos sainz"},
    "george russell": {"russell", "george russell"},
    "sergio perez": {"perez", "sergio perez", "checo"},
    "fernando alonso": {"alonso", "fernando alonso"},
    "oscar piastri": {"piastri", "oscar piastri"},
    "lance stroll": {"stroll", "lance stroll"},
    "esteban ocon": {"ocon", "esteban ocon"},
    "pierre gasly": {"gasly", "pierre gasly"},
    "valtteri bottas": {"bottas", "valtteri bottas"},
    "zhou guanyu": {"zhou", "guanyu zhou"},
    "nico hulkenberg": {"hulkenberg", "nico hulkenberg"},
    "kevin magnussen": {"magnussen", "kevin magnussen"},
    "yuki tsunoda": {"tsunoda", "yuki tsunoda"},
    "daniel ricciardo": {"ricciardo", "daniel ricciardo"},
    "f1 red bull racing": {"red bull racing", "red bull f1", "rbr"},
    "f1 ferrari": {"ferrari f1", "scuderia ferrari"},
    "f1 mercedes": {"mercedes f1", "amg petronas"},
    "f1 mclaren": {"mclaren f1"},
    "f1 aston martin": {"aston martin f1"},
    "f1 alpine": {"alpine f1", "bwt alpine"},
    "f1 williams": {"williams f1", "williams racing"},
    "f1 alphatauri": {"alphatauri", "rb motorsports", "rb f1"},
    "f1 haas": {"haas f1", "haas"},
    "f1 alfa romeo": {"alfa romeo f1", "kick sauber"},

    # -------------------------------------------------------------------------
    # UFC / MMA
    # -------------------------------------------------------------------------
    "jon jones": {"jon jones", "bones jones", "jj ufc"},
    "israel adesanya": {"adesanya", "israel adesanya", "last stylebender"},
    "alexander volkanovski": {"volkanovski", "alexander volkanovski"},
    "leon edwards": {"leon edwards"},
    "colby covington": {"covington", "colby covington", "chaos"},
    "kamaru usman": {"usman", "kamaru usman", "nigerian nightmare"},
    "khamzat chimaev": {"chimaev", "khamzat chimaev", "borz"},
    "sean o'malley": {"o'malley", "sean omalley", "suga"},
    "alex pereira": {"pereira", "alex pereira", "poatan"},
    "jiří procházka": {"prochazka", "jiri prochazka"},
    "dricus du plessis": {"du plessis", "dricus du plessis", "stillknocks"},
    "dustin poirier": {"poirier", "dustin poirier", "diamond"},
    "charles oliveira": {"oliveira", "charles oliveira", "do bronx"},
    "islam makhachev": {"makhachev", "islam makhachev"},
    "ilia topuria": {"topuria", "ilia topuria", "el matador"},
    "conor mcgregor": {"mcgregor", "conor mcgregor", "notorious"},
    "nate diaz": {"nate diaz"},
    "nick diaz": {"nick diaz"},
    "tony ferguson": {"tony ferguson", "el cucuy"},
    "max holloway": {"holloway", "max holloway", "blessed"},
    "stipe miocic": {"miocic", "stipe miocic"},
    "francis ngannou": {"ngannou", "francis ngannou", "predator"},
    "tom aspinall": {"aspinall", "tom aspinall"},
    "ufc": {"ufc", "ultimate fighting championship"},

    # -------------------------------------------------------------------------
    # Boxing
    # -------------------------------------------------------------------------
    "tyson fury": {"fury", "tyson fury", "gypsy king"},
    "oleksandr usyk": {"usyk", "oleksandr usyk"},
    "deontay wilder": {"wilder", "deontay wilder", "bronze bomber"},
    "anthony joshua": {"joshua", "anthony joshua", "aj"},
    "canelo alvarez": {"canelo", "canelo alvarez", "saul alvarez"},
    "terence crawford": {"crawford", "terence crawford", "bud"},
    "errol spence jr": {"spence", "errol spence", "truth"},
    "ryan garcia": {"ryan garcia", "king ry"},
    "gervonta davis": {"gervonta davis", "tank davis", "tank"},
    "shakur stevenson": {"stevenson", "shakur stevenson"},

    # -------------------------------------------------------------------------
    # Cricket
    # -------------------------------------------------------------------------
    "india cricket": {"india", "bcci", "team india", "men in blue"},
    "australia cricket": {"australia cricket", "cricket australia", "baggy green"},
    "england cricket": {"england cricket", "ecb", "three lions cricket"},
    "pakistan cricket": {"pakistan cricket", "pcb", "green shirts"},
    "new zealand cricket": {"new zealand cricket", "nzc", "black caps"},
    "south africa cricket": {"south africa cricket", "cricket sa", "proteas"},
    "west indies cricket": {"west indies", "windies", "cricket west indies"},
    "sri lanka cricket": {"sri lanka cricket", "slc", "lion flag"},
    "bangladesh cricket": {"bangladesh cricket", "bcb"},
    "afghanistan cricket": {"afghanistan cricket", "acb"},
    "ipl": {"ipl", "indian premier league"},
    "virat kohli": {"kohli", "virat kohli", "king kohli"},
    "rohit sharma": {"rohit sharma", "hitman"},
    "ms dhoni": {"dhoni", "ms dhoni", "msd", "captain cool"},
    "ben stokes": {"stokes", "ben stokes"},
    "pat cummins": {"cummins", "pat cummins"},
    "babar azam": {"babar", "babar azam"},

    # -------------------------------------------------------------------------
    # Rugby
    # -------------------------------------------------------------------------
    "england rugby": {"england rugby", "red roses rugby"},
    "ireland rugby": {"ireland rugby", "irfu"},
    "france rugby": {"france rugby", "les bleus rugby", "ffr"},
    "wales rugby": {"wales rugby", "welsh rugby", "wru"},
    "scotland rugby": {"scotland rugby", "sru"},
    "italy rugby": {"italy rugby", "fir"},
    "new zealand rugby": {"new zealand rugby", "all blacks", "nzru"},
    "south africa rugby": {"south africa rugby", "springboks", "saru"},
    "australia rugby": {"australia rugby", "wallabies", "rugby australia"},
    "argentina rugby": {"argentina rugby", "pumas", "uar"},
    "six nations": {"six nations", "6 nations"},
    "rugby world cup": {"rugby world cup", "rwc"},

    # -------------------------------------------------------------------------
    # NCAA / College Sports
    # -------------------------------------------------------------------------
    "alabama crimson tide": {"alabama", "crimson tide", "tide", "bama"},
    "ohio state buckeyes": {"ohio state", "buckeyes", "osu"},
    "georgia bulldogs": {"georgia bulldogs", "uga"},
    "michigan wolverines": {"michigan wolverines", "wolverines"},
    "clemson tigers": {"clemson tigers", "clemson"},
    "lsu tigers": {"lsu tigers", "lsu"},
    "notre dame fighting irish": {"notre dame", "fighting irish", "nd"},
    "texas longhorns": {"texas longhorns", "longhorns", "ut"},
    "penn state nittany lions": {"penn state", "nittany lions", "psu"},
    "duke blue devils": {"duke blue devils", "duke"},
    "kentucky wildcats": {"kentucky wildcats", "kentucky"},
    "kansas jayhawks": {"kansas jayhawks", "kansas"},
    "march madness": {"march madness", "ncaa tournament", "ncaa basketball tournament"},
    "college football playoff": {"cfp", "college football playoff"},
}

CRYPTO: Dict[str, Set[str]] = {
    # Top cryptocurrencies by market cap with full names, symbols, aliases
    "bitcoin": {"bitcoin", "btc", "xbt", "satoshi", "digital gold"},
    "ethereum": {"ethereum", "eth", "ether"},
    "tether": {"tether", "usdt", "tether usd"},
    "bnb": {"bnb", "binance coin", "binance bnb", "build and build"},
    "solana": {"solana", "sol"},
    "usd coin": {"usd coin", "usdc", "circle usdc"},
    "xrp": {"xrp", "ripple", "ripple xrp"},
    "lido staked ether": {"steth", "lido staked ether", "lido eth"},
    "dogecoin": {"dogecoin", "doge"},
    "tron": {"tron", "trx"},
    "cardano": {"cardano", "ada"},
    "avalanche": {"avalanche", "avax"},
    "wrapped bitcoin": {"wrapped bitcoin", "wbtc"},
    "shiba inu": {"shiba inu", "shib"},
    "polkadot": {"polkadot", "dot"},
    "chainlink": {"chainlink", "link"},
    "toncoin": {"toncoin", "ton", "the open network"},
    "bitcoin cash": {"bitcoin cash", "bch"},
    "near protocol": {"near protocol", "near"},
    "litecoin": {"litecoin", "ltc"},
    "uniswap": {"uniswap", "uni"},
    "dai": {"dai", "makerdao dai"},
    "wrapped ether": {"wrapped ether", "weth"},
    "internet computer": {"internet computer", "icp"},
    "leo token": {"leo token", "leo", "bitfinex leo"},
    "ethereum classic": {"ethereum classic", "etc"},
    "aptos": {"aptos", "apt"},
    "stellar": {"stellar", "xlm", "stellar lumens"},
    "cosmos": {"cosmos", "atom"},
    "monero": {"monero", "xmr"},
    "okb": {"okb", "okx token"},
    "filecoin": {"filecoin", "fil"},
    "hedera": {"hedera", "hbar", "hedera hashgraph"},
    "crypto com coin": {"crypto.com coin", "cro"},
    "vechain": {"vechain", "vet"},
    "lido dao": {"lido dao", "ldo"},
    "immutable x": {"immutable x", "imx"},
    "theta network": {"theta network", "theta"},
    "algorand": {"algorand", "algo"},
    "quant": {"quant network", "qnt"},
    "flow": {"flow blockchain", "flow"},
    "aave": {"aave", "aave protocol"},
    "the sandbox": {"the sandbox", "sand"},
    "decentraland": {"decentraland", "mana"},
    "gala": {"gala", "gala games"},
    "axie infinity": {"axie infinity", "axs"},
    "enjin coin": {"enjin coin", "enj"},
    "bitcoin sv": {"bitcoin sv", "bsv"},
    "zcash": {"zcash", "zec"},
    "maker": {"maker", "mkr", "makerdao"},
    "fantom": {"fantom", "ftm"},
    "arweave": {"arweave", "ar"},
    "the graph": {"the graph", "grt"},
    "compound": {"compound", "comp"},
    "curve dao": {"curve dao", "crv", "curve finance"},
    "synthetix": {"synthetix", "snx"},
    "yearn finance": {"yearn finance", "yfi"},
    "sushiswap": {"sushiswap", "sushi"},
    "1inch": {"1inch", "1inch network"},
    "pancakeswap": {"pancakeswap", "cake"},
    "mina protocol": {"mina protocol", "mina"},
    "helium": {"helium", "hnt"},
    "eos": {"eos", "eosio"},
    "iota": {"iota", "miota"},
    "neo": {"neo", "neo blockchain"},
    "waves": {"waves", "waves platform"},
    "tezos": {"tezos", "xtz"},
    "zilliqa": {"zilliqa", "zil"},
    "dash": {"dash", "dash coin"},
    "decred": {"decred", "dcr"},
    "ravencoin": {"ravencoin", "rvn"},
    "horizen": {"horizen", "zen"},
    "0x": {"0x", "zrx"},
    "basic attention token": {"basic attention token", "bat"},
    "civic": {"civic", "cvc"},
    "kyber network": {"kyber network", "knc"},
    "bancor": {"bancor", "bnt"},
    "storj": {"storj"},
    "augur": {"augur", "rep"},
    "loopring": {"loopring", "lrc"},
    "status": {"status", "snt"},
    "golem": {"golem", "glm"},
    "gnosis": {"gnosis", "gno"},
    "numeraire": {"numeraire", "nmr"},
    "ren": {"ren", "renvm"},
    "district0x": {"district0x", "dnt"},
    "band protocol": {"band protocol", "band"},
    "origin protocol": {"origin protocol", "ogn"},
    "ocean protocol": {"ocean protocol", "ocean"},
    "fetch ai": {"fetch.ai", "fet"},
    "ankr": {"ankr"},
    "celer network": {"celer network", "celr"},
    "api3": {"api3"},
    "livepeer": {"livepeer", "lpt"},
    "audius": {"audius", "audio"},
    "rally": {"rally", "rly"},
    "uma": {"uma", "universal market access"},
    "badger dao": {"badger dao", "badger"},
    "perpetual protocol": {"perpetual protocol", "perp"},
    "mirror protocol": {"mirror protocol", "mir"},
    "terra luna": {"terra luna", "luna", "terra"},
    "terrausd": {"terrausd", "ust"},
    "frax": {"frax", "frax finance"},
    "frax share": {"frax share", "fxs"},
    "amp": {"amp token", "amp"},
    "holo": {"holo", "hot"},
    "ontology": {"ontology", "ont"},
    "nervos network": {"nervos network", "ckb"},
    "harmony": {"harmony", "one"},
    "celo": {"celo"},
    "elrond": {"elrond", "egld", "multiversx"},
    "kava": {"kava"},
    "icon": {"icon", "icx"},
    "wax": {"wax", "waxp"},
    "huobi token": {"huobi token", "ht"},
    "gate token": {"gate token", "gt"},
    "mx token": {"mx token", "mx"},
    "serum": {"serum", "srm"},
    "raydium": {"raydium", "ray"},
    "step finance": {"step finance", "step"},
    "mango markets": {"mango markets", "mngo"},
    "magic internet money": {"magic internet money", "mim"},
    "spell token": {"spell token", "spell"},
    "wonderland": {"wonderland", "time"},
    "olympus dao": {"olympus dao", "ohm"},
    "joe token": {"joe token", "joe"},
    "aurora": {"aurora", "aoa"},
    "moonbeam": {"moonbeam", "glmr"},
    "moonriver": {"moonriver", "movr"},
    "kusama": {"kusama", "ksm"},
    "acala": {"acala", "aca"},
    "astar": {"astar network", "astr"},
    "phala network": {"phala network", "pha"},
    "crust network": {"crust network", "cru"},
    "bifrost": {"bifrost", "bnc"},
    "klaytn": {"klaytn", "klay"},
    "terra luna classic": {"terra luna classic", "lunc"},
    "sei": {"sei network", "sei"},
    "sui": {"sui", "sui network"},
    "blur": {"blur", "blur nft"},
    "arbitrum": {"arbitrum", "arb"},
    "optimism": {"optimism", "op"},
    "polygon": {"polygon", "matic", "matic network"},
    "stacks": {"stacks", "stx"},
    "injective": {"injective", "inj"},
    "render": {"render", "rndr", "render token"},
    "mantle": {"mantle", "mnt"},
    "floki": {"floki", "floki inu"},
    "pepe": {"pepe", "pepe coin"},
    "wif": {"wif", "dogwifhat", "dogwifcoin"},
    "bonk": {"bonk"},
    "jupitar": {"jupiter", "jup"},
    "pyth network": {"pyth network", "pyth"},
    "celestia": {"celestia", "tia"},
    "dydx": {"dydx"},
    "worldcoin": {"worldcoin", "wld"},
    "pendle": {"pendle"},
    "ethena": {"ethena", "ena"},
    "notcoin": {"notcoin", "not"},
    "brett": {"brett", "brett coin"},
    "base": {"base", "base chain", "coinbase base"},
}

POLITICS: Dict[str, Set[str]] = {
    # -------------------------------------------------------------------------
    # US Federal - Executive
    # -------------------------------------------------------------------------
    "donald trump": {"trump", "donald trump", "president trump", "45th president", "47th president", "donald j trump"},
    "joe biden": {"biden", "joe biden", "president biden", "46th president", "joseph biden"},
    "kamala harris": {"kamala harris", "kamala", "vice president harris", "vp harris"},
    "jd vance": {"jd vance", "j.d. vance", "vance", "vice president vance"},
    "mike pence": {"pence", "mike pence", "michael pence"},
    "hillary clinton": {"hillary clinton", "hillary", "clinton"},
    "barack obama": {"obama", "barack obama", "president obama", "44th president"},
    "george w bush": {"bush", "george w bush", "george bush", "43rd president", "gwb"},
    "bill clinton": {"bill clinton", "william clinton", "42nd president"},
    "george h w bush": {"george h.w. bush", "george hw bush", "41st president"},

    # -------------------------------------------------------------------------
    # US Federal - Congress
    # -------------------------------------------------------------------------
    "mitch mcconnell": {"mcconnell", "mitch mcconnell", "senate minority leader"},
    "chuck schumer": {"schumer", "chuck schumer", "senate majority leader"},
    "nancy pelosi": {"pelosi", "nancy pelosi", "speaker pelosi"},
    "kevin mccarthy": {"mccarthy", "kevin mccarthy", "speaker mccarthy"},
    "mike johnson": {"mike johnson", "speaker johnson"},
    "hakeem jeffries": {"jeffries", "hakeem jeffries"},
    "bernie sanders": {"bernie sanders", "bernie", "senator sanders"},
    "elizabeth warren": {"warren", "elizabeth warren", "senator warren"},
    "aoc": {"aoc", "alexandria ocasio-cortez", "ocasio-cortez"},
    "rand paul": {"rand paul", "senator rand paul"},
    "marco rubio": {"rubio", "marco rubio", "senator rubio"},
    "ted cruz": {"ted cruz", "senator cruz"},
    "mitt romney": {"romney", "mitt romney"},
    "lindsey graham": {"graham", "lindsey graham"},
    "adam schiff": {"schiff", "adam schiff", "senator schiff"},
    "ron wyden": {"wyden", "ron wyden"},
    "john fetterman": {"fetterman", "john fetterman", "senator fetterman"},
    "gavin newsom": {"newsom", "gavin newsom", "governor newsom"},
    "ron desantis": {"desantis", "ron desantis", "governor desantis"},
    "nikki haley": {"haley", "nikki haley"},
    "vivek ramaswamy": {"ramaswamy", "vivek ramaswamy", "vivek"},
    "pete buttigieg": {"buttigieg", "pete buttigieg", "mayor pete", "secretary buttigieg"},
    "pete hegseth": {"hegseth", "pete hegseth"},

    # -------------------------------------------------------------------------
    # US Cabinet & Key Officials
    # -------------------------------------------------------------------------
    "janet yellen": {"yellen", "janet yellen", "treasury secretary yellen"},
    "jerome powell": {"powell", "jerome powell", "fed chair powell"},
    "anthony fauci": {"fauci", "anthony fauci"},
    "lloyd austin": {"austin", "lloyd austin", "secretary austin"},
    "antony blinken": {"blinken", "antony blinken", "secretary blinken"},
    "scott bessent": {"bessent", "scott bessent"},
    "elon musk": {"elon musk", "elon", "musk", "doge chief", "spacex", "tesla ceo", "x owner"},
    "rfk jr": {"rfk jr", "robert f kennedy jr", "kennedy"},

    # -------------------------------------------------------------------------
    # International Leaders - G20 & Major Nations
    # -------------------------------------------------------------------------
    "vladimir putin": {"putin", "vladimir putin", "president putin"},
    "xi jinping": {"xi jinping", "xi", "president xi"},
    "emmanuel macron": {"macron", "emmanuel macron", "president macron"},
    "olaf scholz": {"scholz", "olaf scholz", "chancellor scholz"},
    "rishi sunak": {"sunak", "rishi sunak", "pm sunak"},
    "keir starmer": {"starmer", "keir starmer", "pm starmer"},
    "giorgia meloni": {"meloni", "giorgia meloni", "pm meloni"},
    "pedro sanchez": {"sanchez", "pedro sanchez", "pm sanchez"},
    "mark carney": {"carney", "mark carney", "pm carney"},
    "justin trudeau": {"trudeau", "justin trudeau", "pm trudeau"},
    "yoon suk-yeol": {"yoon", "yoon suk-yeol", "yoon suk yeol", "president yoon"},
    "fumio kishida": {"kishida", "fumio kishida", "pm kishida"},
    "shigeru ishiba": {"ishiba", "shigeru ishiba"},
    "narendra modi": {"modi", "narendra modi", "pm modi"},
    "lula": {"lula", "lula da silva", "luiz inácio lula da silva", "president lula"},
    "jair bolsonaro": {"bolsonaro", "jair bolsonaro"},
    "andrés manuel lópez obrador": {"amlo", "lopez obrador", "andrés manuel lópez obrador"},
    "claudia sheinbaum": {"sheinbaum", "claudia sheinbaum", "president sheinbaum"},
    "javier milei": {"milei", "javier milei", "president milei"},
    "cyril ramaphosa": {"ramaphosa", "cyril ramaphosa", "president ramaphosa"},
    "anthony albanese": {"albanese", "anthony albanese", "pm albanese"},
    "jacinda ardern": {"ardern", "jacinda ardern"},
    "christopher luxon": {"luxon", "christopher luxon"},
    "recep tayyip erdogan": {"erdogan", "recep erdogan", "president erdogan"},
    "benjamin netanyahu": {"netanyahu", "benjamin netanyahu", "bibi", "pm netanyahu"},
    "volodymyr zelensky": {"zelensky", "volodymyr zelensky", "president zelensky"},
    "kim jong-un": {"kim jong-un", "kim jong un", "north korea leader"},
    "ali khamenei": {"khamenei", "ali khamenei", "supreme leader"},
    "ursula von der leyen": {"von der leyen", "ursula von der leyen", "ec president"},
    "alexander lukashenko": {"lukashenko", "alexander lukashenko"},
    "abdel fattah el-sisi": {"el-sisi", "sisi", "president sisi"},
    "muhammed bin salman": {"mbs", "mohammed bin salman", "crown prince mbs"},
    "king salman": {"king salman", "salman bin abdulaziz"},
    "imran khan": {"imran khan"},
    "shehbaz sharif": {"shehbaz sharif", "pm sharif"},

    # -------------------------------------------------------------------------
    # US Political Parties & Bodies
    # -------------------------------------------------------------------------
    "republican party": {"republican", "republicans", "gop", "rnc", "republican party"},
    "democratic party": {"democrat", "democrats", "dnc", "democratic party"},
    "us senate": {"senate", "us senate", "united states senate"},
    "us house": {"house", "us house", "house of representatives", "house representatives"},
    "us congress": {"congress", "us congress", "united states congress"},
    "supreme court": {"supreme court", "scotus", "us supreme court"},
    "white house": {"white house"},
    "state department": {"state department", "dos"},
    "pentagon": {"pentagon", "dod", "department of defense"},
    "cia": {"cia", "central intelligence agency"},
    "fbi": {"fbi", "federal bureau of investigation"},
    "doge": {"doge department", "department of government efficiency"},

    # -------------------------------------------------------------------------
    # International Political Bodies
    # -------------------------------------------------------------------------
    "united nations": {"united nations", "un", "unga", "unsc"},
    "nato": {"nato", "north atlantic treaty organization"},
    "european union": {"eu", "european union"},
    "world health organization": {"who", "world health organization"},
    "world trade organization": {"wto", "world trade organization"},
    "g7": {"g7", "group of seven"},
    "g20": {"g20", "group of twenty"},
    "brics": {"brics"},
    "imf": {"imf", "international monetary fund"},
    "world bank": {"world bank"},
}

ECONOMICS: Dict[str, Set[str]] = {
    # -------------------------------------------------------------------------
    # Central Banks & Key Officials
    # -------------------------------------------------------------------------
    "federal reserve": {"fed", "federal reserve", "fomc", "federal open market committee", "us central bank"},
    "jerome powell": {"powell", "fed chair", "jerome powell"},
    "european central bank": {"ecb", "european central bank", "lagarde"},
    "christine lagarde": {"lagarde", "christine lagarde"},
    "bank of japan": {"boj", "bank of japan"},
    "kazuo ueda": {"ueda", "kazuo ueda", "boj governor"},
    "bank of england": {"boe", "bank of england"},
    "andrew bailey": {"bailey", "andrew bailey"},
    "bank of canada": {"boc", "bank of canada"},
    "reserve bank of australia": {"rba", "reserve bank of australia"},
    "peoples bank of china": {"pboc", "peoples bank of china", "people's bank of china"},
    "swiss national bank": {"snb", "swiss national bank"},

    # -------------------------------------------------------------------------
    # Key Economic Indicators
    # -------------------------------------------------------------------------
    "consumer price index": {"cpi", "consumer price index", "inflation report", "inflation data"},
    "pce": {"pce", "personal consumption expenditures", "core pce"},
    "nonfarm payrolls": {"nfp", "nonfarm payrolls", "jobs report", "employment report", "payrolls"},
    "unemployment rate": {"unemployment rate", "unemployment", "jobless rate"},
    "gdp": {"gdp", "gross domestic product", "economic growth"},
    "pmi": {"pmi", "purchasing managers index", "manufacturing pmi", "services pmi", "ism"},
    "retail sales": {"retail sales", "consumer spending"},
    "housing starts": {"housing starts", "housing data"},
    "durable goods orders": {"durable goods orders", "durable goods"},
    "trade balance": {"trade balance", "trade deficit", "trade surplus"},
    "interest rate decision": {"interest rate decision", "rate decision", "rate hike", "rate cut", "basis points"},
    "federal funds rate": {"federal funds rate", "fed funds rate", "ffr"},
    "treasury yield": {"treasury yield", "10 year yield", "2 year yield", "yield curve", "10yr"},
    "inflation": {"inflation", "deflation", "disinflation", "stagflation"},
    "recession": {"recession", "economic downturn", "gdp contraction"},
    "debt ceiling": {"debt ceiling", "debt limit", "us debt"},
    "quantitative easing": {"qe", "quantitative easing", "qt", "quantitative tightening"},
    "tapering": {"tapering", "asset purchase"},
    "yield curve": {"yield curve", "yield inversion", "inverted yield"},

    # -------------------------------------------------------------------------
    # Stock Market Indices
    # -------------------------------------------------------------------------
    "s&p 500": {"s&p 500", "s&p500", "sp500", "spx", "s&p", "snp500"},
    "dow jones": {"dow jones", "dow", "djia", "dow jones industrial average"},
    "nasdaq": {"nasdaq", "nasdaq composite", "qqq", "tech stocks"},
    "russell 2000": {"russell 2000", "rut", "small cap"},
    "vix": {"vix", "cboe vix", "volatility index", "fear index"},
    "nikkei": {"nikkei", "nikkei 225", "japan stocks"},
    "ftse": {"ftse", "ftse 100", "footsie", "uk stocks"},
    "dax": {"dax", "german stocks", "frankfurt stocks"},
    "cac 40": {"cac 40", "cac", "french stocks"},
    "shanghai composite": {"shanghai composite", "ssec", "china stocks"},
    "hang seng": {"hang seng", "hsi", "hong kong stocks"},
    "sensex": {"sensex", "bse sensex", "india stocks"},

    # -------------------------------------------------------------------------
    # Major Companies / Stocks
    # -------------------------------------------------------------------------
    "apple": {"apple", "aapl", "apple inc", "apple stock"},
    "microsoft": {"microsoft", "msft", "microsoft stock"},
    "google": {"google", "alphabet", "googl", "goog", "alphabet inc"},
    "amazon": {"amazon", "amzn", "amazon stock"},
    "meta": {"meta", "meta platforms", "facebook", "fb", "meta stock"},
    "tesla": {"tesla", "tsla", "tesla stock", "tesla motors"},
    "nvidia": {"nvidia", "nvda", "nvidia stock"},
    "berkshire hathaway": {"berkshire hathaway", "brk", "warren buffett company"},
    "jpmorgan": {"jpmorgan", "jp morgan", "jpm", "jpmorgan chase"},
    "visa": {"visa", "v stock"},
    "johnson & johnson": {"j&j", "jnj", "johnson johnson"},
    "walmart": {"walmart", "wmt"},
    "exxon mobil": {"exxon mobil", "exxon", "xom"},
    "unitedhealth": {"unitedhealth", "unh"},
    "procter gamble": {"procter gamble", "pg stock"},
    "chevron": {"chevron", "cvx"},
    "home depot": {"home depot", "hd stock"},
    "abbvie": {"abbvie", "abbv"},
    "salesforce": {"salesforce", "crm"},
    "arm holdings": {"arm holdings", "arm"},
    "openai": {"openai", "chatgpt"},
    "spacex": {"spacex", "elon musk spacex"},
    "palantir": {"palantir", "pltr"},
    "coinbase": {"coinbase", "coin stock"},
    "robinhood": {"robinhood", "hood stock"},

    # -------------------------------------------------------------------------
    # Commodities
    # -------------------------------------------------------------------------
    "crude oil": {"crude oil", "wti", "west texas intermediate", "brent crude", "oil price"},
    "natural gas": {"natural gas", "natgas", "ng price"},
    "gold": {"gold", "xau", "gold price"},
    "silver": {"silver", "xag", "silver price"},
    "copper": {"copper", "hg"},
    "wheat": {"wheat", "wheat price"},
    "corn": {"corn", "corn price"},
    "soybeans": {"soybeans", "soy"},
    "cotton": {"cotton"},
    "coffee": {"coffee"},
    "sugar": {"sugar"},
    "lumber": {"lumber"},

    # -------------------------------------------------------------------------
    # Forex
    # -------------------------------------------------------------------------
    "eurusd": {"eurusd", "euro dollar", "eur usd", "eur/usd"},
    "gbpusd": {"gbpusd", "cable", "gbp usd", "gbp/usd", "pound dollar"},
    "usdjpy": {"usdjpy", "dollar yen", "usd jpy", "usd/jpy"},
    "usdcad": {"usdcad", "usd cad", "usd/cad", "loonie"},
    "audusd": {"audusd", "aud usd", "aud/usd", "aussie dollar"},
    "usdchf": {"usdchf", "usd chf", "swiss franc"},
    "dollar index": {"dollar index", "dxy", "usd index"},
}

WEATHER: Dict[str, Set[str]] = {
    # -------------------------------------------------------------------------
    # Storm Types
    # -------------------------------------------------------------------------
    "hurricane": {"hurricane", "tropical storm", "tropical cyclone", "named storm"},
    "typhoon": {"typhoon", "western pacific storm"},
    "cyclone": {"cyclone", "tropical cyclone", "indian ocean cyclone"},
    "tornado": {"tornado", "twister", "tornado warning", "tornado watch"},
    "blizzard": {"blizzard", "winter storm", "snowstorm", "nor'easter", "noreaster"},
    "nor'easter": {"nor'easter", "noreaster", "northeast storm"},
    "derecho": {"derecho", "straight-line winds"},
    "dust storm": {"dust storm", "haboob", "sandstorm"},
    "ice storm": {"ice storm", "freezing rain", "sleet storm"},
    "heatwave": {"heatwave", "heat wave", "heat dome", "extreme heat"},
    "drought": {"drought", "dry conditions"},
    "flood": {"flood", "flooding", "flash flood", "river flood"},
    "wildfire": {"wildfire", "forest fire", "brush fire", "bushfire"},
    "earthquake": {"earthquake", "quake", "seismic", "temblor"},
    "tsunami": {"tsunami", "tidal wave"},
    "volcano": {"volcano", "volcanic eruption", "lava flow"},
    "landslide": {"landslide", "mudslide", "debris flow"},
    "avalanche": {"avalanche", "snow slide"},

    # -------------------------------------------------------------------------
    # NOAA / NHC Hurricane Categories
    # -------------------------------------------------------------------------
    "category 1 hurricane": {"category 1", "cat 1 hurricane", "cat-1"},
    "category 2 hurricane": {"category 2", "cat 2 hurricane", "cat-2"},
    "category 3 hurricane": {"category 3", "cat 3 hurricane", "cat-3", "major hurricane"},
    "category 4 hurricane": {"category 4", "cat 4 hurricane", "cat-4"},
    "category 5 hurricane": {"category 5", "cat 5 hurricane", "cat-5"},

    # -------------------------------------------------------------------------
    # Weather Organizations
    # -------------------------------------------------------------------------
    "noaa": {"noaa", "national oceanic atmospheric", "national weather service", "nws"},
    "nhc": {"nhc", "national hurricane center"},
    "national hurricane center": {"national hurricane center", "nhc"},
    "fema": {"fema", "federal emergency management"},

    # -------------------------------------------------------------------------
    # Weather-Prone Regions / US
    # -------------------------------------------------------------------------
    "gulf coast": {"gulf coast", "gulf of mexico"},
    "atlantic hurricane season": {"atlantic hurricane season", "hurricane season"},
    "tornado alley": {"tornado alley"},
    "florida weather": {"florida", "florida hurricane"},
    "texas weather": {"texas weather", "texas hurricane", "texas tornado"},
    "california wildfire": {"california wildfire", "california fire", "socal fire"},
    "pacific northwest weather": {"pacific northwest", "pnw weather"},

    # -------------------------------------------------------------------------
    # Named Storms (Recent / Memorable)
    # -------------------------------------------------------------------------
    "hurricane ian": {"hurricane ian", "ian"},
    "hurricane katrina": {"hurricane katrina", "katrina"},
    "hurricane irma": {"hurricane irma", "irma"},
    "hurricane maria": {"hurricane maria", "maria"},
    "hurricane harvey": {"hurricane harvey", "harvey"},
    "hurricane dorian": {"hurricane dorian", "dorian"},
    "hurricane ida": {"hurricane ida", "ida"},
    "hurricane helene": {"hurricane helene", "helene"},
    "hurricane milton": {"hurricane milton", "milton"},

    # -------------------------------------------------------------------------
    # Climate / Environment
    # -------------------------------------------------------------------------
    "el nino": {"el nino", "el niño", "enso", "la nina", "la niña"},
    "climate change": {"climate change", "global warming"},
    "paris agreement": {"paris agreement", "paris accord"},
    "cop": {"cop28", "cop29", "cop30", "un climate conference"},
}

GENERAL: Dict[str, Set[str]] = {
    # -------------------------------------------------------------------------
    # Awards Shows
    # -------------------------------------------------------------------------
    "academy awards": {"oscars", "academy awards", "oscar", "oscar ceremony", "oscar award"},
    "grammy awards": {"grammys", "grammy awards", "grammy"},
    "emmy awards": {"emmys", "emmy awards", "emmy"},
    "golden globe awards": {"golden globes", "golden globe awards", "ggaa"},
    "tony awards": {"tonys", "tony awards"},
    "bafta": {"bafta", "bafta awards", "british academy"},
    "cannes film festival": {"cannes", "cannes film festival", "palme d'or"},
    "sundance film festival": {"sundance"},
    "vma awards": {"vma", "mtv vmas", "mtv video music awards"},
    "billboard music awards": {"billboard music awards", "bbma"},
    "american music awards": {"amas", "american music awards"},
    "peoples choice awards": {"peoples choice", "people's choice awards"},
    "mtv awards": {"mtv awards"},

    # -------------------------------------------------------------------------
    # Entertainment
    # -------------------------------------------------------------------------
    "super bowl halftime show": {"super bowl halftime", "halftime show"},
    "super bowl": {"super bowl", "big game"},
    "taylor swift": {"taylor swift", "swift", "eras tour"},
    "beyonce": {"beyonce", "beyoncé"},
    "drake": {"drake", "aubrey graham"},
    "kendrick lamar": {"kendrick lamar", "kendrick"},
    "bad bunny": {"bad bunny", "benito"},
    "the weeknd": {"the weeknd", "abel"},
    "rihanna": {"rihanna"},
    "ariana grande": {"ariana grande", "ariana"},
    "billie eilish": {"billie eilish"},
    "olivia rodrigo": {"olivia rodrigo"},
    "doja cat": {"doja cat"},
    "kanye west": {"kanye west", "ye"},
    "jay z": {"jay z", "jay-z", "shawn carter"},
    "eminem": {"eminem", "slim shady"},
    "adele": {"adele"},
    "ed sheeran": {"ed sheeran"},
    "harry styles": {"harry styles"},
    "spotify": {"spotify", "spotify wrapped"},
    "netflix": {"netflix", "nflx"},
    "apple tv": {"apple tv", "apple tv+"},
    "hbo": {"hbo", "hbo max", "max streaming"},
    "disney plus": {"disney plus", "disney+"},
    "amazon prime video": {"amazon prime video", "prime video"},
    "marvel": {"marvel", "mcu", "marvel cinematic universe"},
    "star wars": {"star wars"},
    "avatar": {"avatar", "james cameron avatar"},

    # -------------------------------------------------------------------------
    # Space / Science
    # -------------------------------------------------------------------------
    "nasa": {"nasa", "national aeronautics and space administration"},
    "spacex": {"spacex", "falcon 9", "starship"},
    "blue origin": {"blue origin", "new shepard", "jeff bezos space"},
    "rocket lab": {"rocket lab"},
    "iss": {"iss", "international space station"},
    "starship": {"starship", "super heavy"},
    "artemis": {"artemis", "artemis mission", "lunar mission"},
    "moon mission": {"moon mission", "lunar landing"},
    "mars mission": {"mars mission", "mars exploration"},
    "james webb telescope": {"james webb", "jwst", "webb telescope"},
    "hubble": {"hubble", "hubble telescope"},

    # -------------------------------------------------------------------------
    # Tech / AI
    # -------------------------------------------------------------------------
    "artificial intelligence": {"artificial intelligence", "ai", "machine learning", "ml"},
    "chatgpt": {"chatgpt", "openai chatgpt", "gpt-4", "gpt4", "gpt"},
    "claude ai": {"claude", "claude ai", "anthropic"},
    "anthropic": {"anthropic"},
    "gemini ai": {"gemini", "google gemini", "bard"},
    "llama": {"llama", "meta llama", "llama 3"},
    "apple intelligence": {"apple intelligence", "apple ai"},
    "microsoft copilot": {"copilot", "microsoft copilot"},
    "google": {"google", "alphabet google"},
    "apple": {"apple", "apple inc", "tim cook"},
    "microsoft": {"microsoft", "satya nadella"},
    "amazon": {"amazon", "andy jassy", "aws"},
    "meta": {"meta", "facebook", "mark zuckerberg"},
    "nvidia": {"nvidia", "jensen huang"},
    "samsung": {"samsung"},
    "qualcomm": {"qualcomm"},
    "intel": {"intel"},
    "amd": {"amd", "advanced micro devices"},
    "iphone": {"iphone"},
    "android": {"android"},
    "cybertruck": {"cybertruck"},
    "vision pro": {"vision pro", "apple vision pro"},

    # -------------------------------------------------------------------------
    # World Events / International
    # -------------------------------------------------------------------------
    "ukraine war": {"ukraine war", "russia ukraine", "ukraine conflict", "ukraine invasion"},
    "russia sanctions": {"russia sanctions", "sanctions russia"},
    "middle east conflict": {"middle east conflict", "israel hamas", "gaza", "israel war"},
    "taiwan strait": {"taiwan strait", "taiwan china", "taiwan conflict"},
    "north korea nuclear": {"north korea nuclear", "north korea missile", "dprk"},
    "iran nuclear": {"iran nuclear", "iran deal", "jcpoa"},
    "us china trade": {"us china trade", "trade war", "tariffs china"},
    "covid": {"covid", "covid-19", "coronavirus", "pandemic", "omicron"},
    "monkeypox": {"monkeypox", "mpox"},

    # -------------------------------------------------------------------------
    # US Domestic Events
    # -------------------------------------------------------------------------
    "midterm elections": {"midterm elections", "midterms"},
    "presidential election": {"presidential election", "election day", "election 2024", "election 2028"},
    "state of the union": {"state of the union", "sotu"},
    "inauguration": {"inauguration", "inauguration day"},
    "government shutdown": {"government shutdown", "shutdown", "debt ceiling shutdown"},
    "supreme court ruling": {"supreme court ruling", "scotus ruling", "supreme court decision"},
    "impeachment": {"impeachment", "impeach"},
    "january 6": {"january 6", "jan 6", "capitol attack"},

    # -------------------------------------------------------------------------
    # International Sports Events
    # -------------------------------------------------------------------------
    "summer olympics": {"summer olympics", "olympic games", "paris olympics", "la28 olympics"},
    "winter olympics": {"winter olympics", "beijing olympics", "milan olympics"},
    "fifa world cup": {"world cup", "fifa world cup", "soccer world cup"},
    "euro cup": {"euro cup", "euro 2024", "euros", "european championship"},
    "copa america": {"copa america"},
    "africa cup of nations": {"afcon", "africa cup of nations"},
    "ryder cup": {"ryder cup"},
    "world series": {"world series", "baseball world series"},
    "nba finals": {"nba finals"},
    "stanley cup": {"stanley cup", "stanley cup finals"},
    "nfl super bowl": {"super bowl", "nfl championship"},
    "march madness": {"march madness", "ncaa tournament"},
    "college football playoff": {"college football playoff", "cfp"},
    "the open": {"british open", "the open", "open championship"},
    "wimbledon": {"wimbledon", "wimbledon championships"},

    # -------------------------------------------------------------------------
    # Misc / Finance Events
    # -------------------------------------------------------------------------
    "ipo": {"ipo", "initial public offering", "going public"},
    "fed meeting": {"fed meeting", "fomc meeting", "jackson hole"},
    "earnings season": {"earnings season", "earnings report", "quarterly earnings"},
    "black friday": {"black friday"},
    "jobs report": {"jobs report", "nonfarm payrolls"},
    "inflation data": {"inflation data", "cpi report"},
    "new year": {"new year", "new years", "nye"},
    "thanksgiving": {"thanksgiving"},
}

# ---------------------------------------------------------------------------
# Flat lookup: alias → canonical name, for O(1) title scanning
# ---------------------------------------------------------------------------
_ALIAS_MAP: Dict[str, str] = {}
for _registry in (SPORTS, CRYPTO, POLITICS, ECONOMICS, WEATHER, GENERAL):
    for _canonical, _aliases in _registry.items():
        for _alias in _aliases:
            _ALIAS_MAP[_alias.lower()] = _canonical
        _ALIAS_MAP[_canonical.lower()] = _canonical


import re as _re

# Pre-compile word-boundary patterns for short aliases (≤4 chars) — prevents
# "chi" matching inside "chiefs", "ne" matching inside "new", etc.
_BOUNDARY_PATTERNS: Dict[str, _re.Pattern] = {}
for _alias in list(_ALIAS_MAP.keys()):
    if len(_alias) <= 4:
        _BOUNDARY_PATTERNS[_alias] = _re.compile(
            r"(?<![a-z0-9])" + _re.escape(_alias) + r"(?![a-z0-9])"
        )


def find_entities_in_title(title: str) -> list:
    """
    Return list of canonical entity names found in the market title.
    Long aliases (>4 chars) use simple substring match.
    Short aliases (≤4 chars) require word boundaries to prevent false positives.
    Longest match wins — checked first so "real madrid" beats "madrid" alone.
    """
    t = title.lower()
    found = []
    seen = set()
    consumed_spans: list = []   # track char ranges already claimed by a longer match

    for alias in sorted(_ALIAS_MAP.keys(), key=len, reverse=True):
        canonical = _ALIAS_MAP[alias]
        if canonical in seen:
            continue

        # Find match position
        if len(alias) <= 4:
            m = _BOUNDARY_PATTERNS.get(alias)
            match = m.search(t) if m else None
            if not match:
                continue
            start, end = match.start(), match.end()
        else:
            idx = t.find(alias)
            if idx == -1:
                continue
            start, end = idx, idx + len(alias)

        # Skip if this span is already covered by a longer (higher-priority) match
        if any(s <= start < e or s < end <= e for s, e in consumed_spans):
            continue

        found.append(canonical)
        seen.add(canonical)
        consumed_spans.append((start, end))

    return found


def get_aliases(canonical: str) -> Set[str]:
    """Return all aliases for a canonical entity name."""
    for registry in (SPORTS, CRYPTO, POLITICS, ECONOMICS, WEATHER, GENERAL):
        if canonical in registry:
            return registry[canonical]
    return {canonical}


def get_registry_for_entity(canonical: str) -> str:
    """Return the registry name ('SPORTS', 'CRYPTO', etc.) for a canonical entity."""
    mapping = {
        "SPORTS": SPORTS,
        "CRYPTO": CRYPTO,
        "POLITICS": POLITICS,
        "ECONOMICS": ECONOMICS,
        "WEATHER": WEATHER,
        "GENERAL": GENERAL,
    }
    for name, registry in mapping.items():
        if canonical in registry:
            return name
    return "UNKNOWN"
