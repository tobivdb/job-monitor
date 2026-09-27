"""Deterministic formatting of verified vacancy metadata and sheet identities."""
import html
import re
import unicodedata
from datetime import date

COUNTRIES = {'de': 'Germany', 'deu': 'Germany', 'germany': 'Germany', 'deutschland': 'Germany',
             'ch': 'Switzerland', 'che': 'Switzerland', 'switzerland': 'Switzerland', 'schweiz': 'Switzerland',
             'at': 'Austria', 'aut': 'Austria', 'austria': 'Austria', 'österreich': 'Austria',
             'uk': 'United Kingdom', 'gb': 'United Kingdom', 'united kingdom': 'United Kingdom',
             'us': 'United States', 'usa': 'United States', 'united states': 'United States',
             'fr': 'France', 'france': 'France', 'nl': 'Netherlands', 'netherlands': 'Netherlands',
             'be': 'Belgium', 'belgium': 'Belgium', 'lu': 'Luxembourg', 'luxembourg': 'Luxembourg'}
CITY_GROUPS = {
    'Switzerland': ['Zürich', 'Zug', 'Baar', 'Pfäffikon SZ', 'Kloten', 'Opfikon', 'Basel', 'Bern',
                    'St. Gallen', 'Geneva', 'Lausanne', 'Luzern', 'Winterthur', 'Schwyz'],
    'Germany': ['Frankfurt', 'Eschborn', 'Kronberg', 'Hanau', 'Bad Homburg', 'Wiesbaden', 'Darmstadt',
                'Neu-Isenburg', 'Hamburg', 'Berlin', 'Düsseldorf', 'München', 'Karlsruhe', 'Stuttgart',
                'Köln', 'Bonn', 'Essen', 'Dortmund', 'Hannover', 'Mannheim', 'Heidelberg', 'Bremen'],
    'Austria': ['Wien', 'Salzburg', 'Graz', 'Linz'],
    'United Kingdom': ['London'], 'United States': ['New York'],
    'France': ['Paris'], 'Netherlands': ['Amsterdam'], 'Belgium': ['Brussels'],
}
ALIASES = {'zurich': 'Zürich', 'zuerich': 'Zürich', 'munich': 'München', 'muenchen': 'München',
           'vienna': 'Wien', 'cologne': 'Köln', 'koeln': 'Köln', 'dusseldorf': 'Düsseldorf',
           'duesseldorf': 'Düsseldorf', 'frankfurt am main': 'Frankfurt', 'frankfurt a.m.': 'Frankfurt',
           'st gallen': 'St. Gallen', 'genf': 'Geneva', 'genève': 'Geneva', 'lucerne': 'Luzern'}
CITIES = {city.casefold(): (city, country) for country, cities in CITY_GROUPS.items() for city in cities}
for alias, city in ALIASES.items():
    CITIES[alias] = CITIES[city.casefold()]


def normalize_location(value):
    value = ' '.join(html.unescape(str(value or '')).split()).strip(' ,;')
    if not value:
        return ''
    # Explicit remote country only; 'remote' alone must not acquire a country.
    if re.search(r'\b(remote|home.?office)\b', value, re.I):
        rest = re.sub(r'\b(remote|home.?office)\b|[(),]', ' ', value, flags=re.I).strip().casefold()
        country = COUNTRIES.get(rest)
        return f'Remote ({country})' if country else ''
    found_country = None
    city_text = value
    for alias in sorted(COUNTRIES, key=len, reverse=True):
        match = re.search(r'(?:^|[\s,;(])' + re.escape(alias) + r'\)?$', value, re.I)
        if match:
            found_country = COUNTRIES[alias]
            city_text = value[:match.start()].strip(' ,;(')
            break
    city = CITIES.get(city_text.casefold())
    if city:
        if found_country and found_country != city[1]:
            return ''
        return f'{city[0]}, {city[1]}'
    # Unknown city is accepted only with an explicit country, not a multi-location list.
    if found_country and city_text and not re.search(r'[/;|,]|\band\b|\bund\b', city_text, re.I):
        return f'{city_text}, {found_country}'
    return ''


def posting_location(posting):
    if str(posting.get('jobLocationType', '')).upper() == 'TELECOMMUTE':
        places = posting.get('applicantLocationRequirements', [])
        places = places if isinstance(places, list) else [places]
        countries = {COUNTRIES.get(str(p.get('name', '')).casefold()) for p in places if isinstance(p, dict)} - {None}
        return f'Remote ({next(iter(countries))})' if len(countries) == 1 else ''
    places = posting.get('jobLocation', [])
    places = places if isinstance(places, list) else [places]
    locations = set()
    for place in places:
        address = place.get('address', {}) if isinstance(place, dict) else {}
        if not isinstance(address, dict):
            continue
        country = address.get('addressCountry', '')
        if isinstance(country, dict):
            country = country.get('name', '')
        location = normalize_location(f"{address.get('addressLocality', '')}, {country}".strip(', '))
        if location:
            locations.add(location)
    return next(iter(locations)) if len(locations) == 1 else ''


def posted_date(value):
    text = str(value or '')
    if not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T)', text):
        return 'unknown'
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return 'unknown'


GENDER = re.compile(r'\(?\b(?:[mwfdxh](?:\s*[/|*]\s*[mwfdxh]){1,4}|all genders|all gender|male/female)\b\)?', re.I)
CITY_SUFFIX = re.compile(r'(?:\s*[-–—|,(/]\s*|\s+(?:in\s+)?)(?:' + '|'.join(
    re.escape(city) for city in sorted(CITIES, key=len, reverse=True)
) + r')(?:\s*[,/]\s*(?:Germany|Switzerland|Austria|DE|CH|AT))?\)?\s*$', re.I)


def clean_job_title(title):
    text = GENDER.sub('', html.unescape(str(title or '')))
    text = CITY_SUFFIX.sub('', text)
    return re.sub(r'\s+', ' ', text).strip(' -–—|,()/')


def words(text):
    text = unicodedata.normalize('NFKD', html.unescape(text).casefold())
    return re.findall(r'[^\W_]+', ''.join(c for c in text if not unicodedata.combining(c)))


def employer_title_key(employer, title):
    employer_key = ' '.join(words(employer))
    title_words = words(clean_job_title(title))
    # Word order and the optional 'team' do not distinguish investment associate roles.
    if 'investment' in title_words:
        title_words = [word for word in title_words if word != 'team']
    return (employer_key, tuple(sorted(title_words))) if employer_key and title_words else None
