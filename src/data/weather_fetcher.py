"""
Weather data via wttr.in — free, no API key required.

Used to inform AI decisions on Kalshi weather markets:
  "Will Miami reach 95°F today?"
  "Will it rain in NYC this weekend?"
  "Will the high in Chicago exceed 80°F?"

wttr.in returns JSON with current conditions + 3-day forecast.
"""

import logging
import re
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.weather_fetcher")

_TIMEOUT = httpx.Timeout(8.0)

# Common US cities that appear in Kalshi weather markets
# Maps lowercase name/alias → canonical query string for wttr.in
KNOWN_CITIES: Dict[str, str] = {
    "miami":          "Miami,Florida",
    "new york":       "New+York+City",
    "nyc":            "New+York+City",
    "new york city":  "New+York+City",
    "los angeles":    "Los+Angeles,California",
    "la":             "Los+Angeles,California",
    "chicago":        "Chicago,Illinois",
    "houston":        "Houston,Texas",
    "phoenix":        "Phoenix,Arizona",
    "dallas":         "Dallas,Texas",
    "san francisco":  "San+Francisco,California",
    "sf":             "San+Francisco,California",
    "seattle":        "Seattle,Washington",
    "denver":         "Denver,Colorado",
    "atlanta":        "Atlanta,Georgia",
    "boston":         "Boston,Massachusetts",
    "washington":     "Washington,DC",
    "dc":             "Washington,DC",
    "las vegas":      "Las+Vegas,Nevada",
    "minneapolis":    "Minneapolis,Minnesota",
    "detroit":        "Detroit,Michigan",
    "nashville":      "Nashville,Tennessee",
    "charlotte":      "Charlotte,North+Carolina",
    "portland":       "Portland,Oregon",
    "austin":         "Austin,Texas",
    "orlando":        "Orlando,Florida",
    "tampa":          "Tampa,Florida",
    "philadelphia":   "Philadelphia,Pennsylvania",
    "san diego":      "San+Diego,California",
    "salt lake":      "Salt+Lake+City,Utah",
    "kansas city":    "Kansas+City,Missouri",
    "new orleans":    "New+Orleans,Louisiana",
    "jacksonville":   "Jacksonville,Florida",
    "memphis":        "Memphis,Tennessee",
    "oklahoma city":  "Oklahoma+City,Oklahoma",
    "raleigh":        "Raleigh,North+Carolina",
    "richmond":       "Richmond,Virginia",
    "cincinnati":     "Cincinnati,Ohio",
    "cleveland":      "Cleveland,Ohio",
    "pittsburgh":     "Pittsburgh,Pennsylvania",
    "st. louis":      "St.+Louis,Missouri",
    "st louis":       "St.+Louis,Missouri",
    "sacramento":     "Sacramento,California",
    "san jose":       "San+Jose,California",
    "indianapolis":   "Indianapolis,Indiana",
    "columbus":       "Columbus,Ohio",
    "buffalo":        "Buffalo,New+York",
    "milwaukee":      "Milwaukee,Wisconsin",
    "baltimore":      "Baltimore,Maryland",
    "anchorage":      "Anchorage,Alaska",
    "honolulu":       "Honolulu,Hawaii",
    "hawaii":         "Honolulu,Hawaii",
    "alaska":         "Anchorage,Alaska",
}

# Weather condition codes from wttr.in → human readable
_WW_CODES: Dict[int, str] = {
    113: "Sunny", 116: "Partly cloudy", 119: "Cloudy", 122: "Overcast",
    143: "Mist", 176: "Patchy rain", 179: "Patchy snow", 182: "Patchy sleet",
    185: "Patchy freezing drizzle", 200: "Thundery outbreaks",
    227: "Blowing snow", 230: "Blizzard", 248: "Fog", 260: "Freezing fog",
    263: "Patchy light drizzle", 266: "Light drizzle", 281: "Freezing drizzle",
    284: "Heavy freezing drizzle", 293: "Patchy light rain", 296: "Light rain",
    299: "Moderate rain", 302: "Heavy rain", 305: "Heavy rain showers",
    308: "Torrential rain", 311: "Light freezing rain", 314: "Moderate freezing rain",
    317: "Light sleet", 320: "Moderate sleet", 323: "Patchy light snow",
    326: "Light snow", 329: "Patchy moderate snow", 332: "Moderate snow",
    335: "Patchy heavy snow", 338: "Heavy snow", 350: "Ice pellets",
    353: "Light rain showers", 356: "Moderate rain showers",
    359: "Torrential rain showers", 362: "Light sleet showers",
    365: "Moderate sleet showers", 368: "Light snow showers",
    371: "Moderate snow showers", 374: "Light ice pellets",
    377: "Moderate ice pellets", 386: "Patchy rain with thunder",
    389: "Heavy rain with thunder", 392: "Patchy snow with thunder",
    395: "Heavy snow with thunder",
}


def extract_cities(title: str) -> List[str]:
    """Find city names mentioned in a market title. Returns wttr.in query strings."""
    t = title.lower()
    found = []
    # Check longest matches first to avoid partial matches (e.g. "new york" before "york")
    for name in sorted(KNOWN_CITIES.keys(), key=len, reverse=True):
        if name in t and KNOWN_CITIES[name] not in found:
            found.append(KNOWN_CITIES[name])
    return found


async def fetch_weather(city_query: str) -> Optional[Dict]:
    """
    Fetch current conditions + 3-day forecast for a city.
    Returns dict with current temp, high/low, condition, precip chance.
    """
    url = f"https://wttr.in/{city_query}?format=j1"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()

        current   = data["current_condition"][0]
        temp_f    = int(current["temp_F"])
        feels_f   = int(current["FeelsLikeF"])
        humidity  = int(current["humidity"])
        condition = current["weatherDesc"][0]["value"]
        wind_mph  = int(current["windspeedMiles"])

        forecasts = []
        for day in data.get("weather", [])[:3]:
            date      = day["date"]
            max_f     = int(day["maxtempF"])
            min_f     = int(day["mintempF"])
            hourly    = day.get("hourly", [])
            # Average precipitation chance across hourly slots
            precip_chances = [int(h.get("chanceofrain", 0)) for h in hourly]
            avg_precip = sum(precip_chances) // len(precip_chances) if precip_chances else 0
            snow_chances = [int(h.get("chanceofsnow", 0)) for h in hourly]
            avg_snow   = sum(snow_chances) // len(snow_chances) if snow_chances else 0
            forecasts.append({
                "date":       date,
                "max_f":      max_f,
                "min_f":      min_f,
                "precip_pct": avg_precip,
                "snow_pct":   avg_snow,
            })

        return {
            "city":       city_query.replace("+", " ").replace(",", ", "),
            "temp_f":     temp_f,
            "feels_f":    feels_f,
            "humidity":   humidity,
            "condition":  condition,
            "wind_mph":   wind_mph,
            "forecast":   forecasts,
        }
    except Exception as e:
        logger.debug("Weather fetch failed for %s: %s", city_query, e)
        return None


def format_weather(weather: Dict) -> str:
    """Format weather data as a compact block for the AI prompt."""
    city = weather["city"]
    lines = [
        f"Weather — {city}:",
        f"  Now: {weather['temp_f']}°F (feels {weather['feels_f']}°F)"
        f"  {weather['condition']}"
        f"  Humidity {weather['humidity']}%"
        f"  Wind {weather['wind_mph']} mph",
    ]
    for f in weather.get("forecast", []):
        precip = f["precip_pct"]
        snow   = f["snow_pct"]
        precip_str = f"  Rain {precip}%" if precip > 5 else ""
        snow_str   = f"  Snow {snow}%"  if snow  > 5 else ""
        lines.append(
            f"  {f['date']}: High {f['max_f']}°F / Low {f['min_f']}°F{precip_str}{snow_str}"
        )
    return "\n".join(lines)
