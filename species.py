"""Species selection independent of the language model."""
from collections import Counter
from secrets import choice

GROUPS = {
    'canids': (
        ('Волк','wolf'), ('Лиса','red fox'), ('Фенек','fennec fox'), ('Шакал','jackal'),
        ('Койот','coyote'), ('Гривистый волк','maned wolf'), ('Песец','arctic fox')),
    'felids': (
        ('Рысь','Eurasian lynx'), ('Снежный барс','snow leopard'), ('Каракал','caracal'),
        ('Сервал','serval'), ('Гепард','cheetah'), ('Тигр','tiger'), ('Ягуар','jaguar'),
        ('Пума','cougar'), ('Манул','Pallas cat'), ('Лев','lion')),
    'mustelids': (
        ('Выдра','otter'), ('Хорёк','ferret'), ('Барсук','badger'),
        ('Росомаха','wolverine'), ('Куница','marten'), ('Горностай','stoat')),
    'rodents': (
        ('Бобёр','beaver'), ('Капибара','capybara'), ('Нутрия','coypu'),
        ('Шиншилла','chinchilla'), ('Белка','squirrel'), ('Сурок','marmot'),
        ('Тушканчик','jerboa'), ('Соня','dormouse')),
    'ungulates': (
        ('Олень','red deer'), ('Лось','moose'), ('Косуля','roe deer'),
        ('Антилопа','antelope'), ('Зебра','zebra'), ('Альпака','alpaca'),
        ('Муфлон','mouflon'), ('Окапи','okapi'), ('Кабан','wild boar')),
    'birds': (
        ('Ворон','raven'), ('Сорока','magpie'), ('Сова','owl'), ('Сокол','falcon'),
        ('Попугай','parrot'), ('Тукан','toucan'), ('Пингвин','penguin'),
        ('Фламинго','flamingo'), ('Журавль','crane'), ('Удод','hoopoe')),
    'reptiles': (
        ('Геккон','gecko'), ('Игуана','iguana'), ('Хамелеон','chameleon'),
        ('Крокодил','crocodile'), ('Варан','monitor lizard'), ('Черепаха','tortoise')),
    'other': (
        ('Енот','raccoon'), ('Красная панда','red panda'), ('Гиена','spotted hyena'),
        ('Опоссум','opossum'), ('Намбат','numbat'), ('Кенгуру','kangaroo'),
        ('Квокка','quokka'), ('Вомбат','wombat'), ('Тапир','tapir'),
        ('Кролик','rabbit'), ('Заяц','hare'), ('Ёж','hedgehog'),
        ('Муравьед','anteater'), ('Мангуст','mongoose'), ('Летучая мышь','bat'),
        ('Медведь','bear'), ('Лемур','lemur')),
}

def species_key(value):
    return ' '.join(str(value).casefold().replace('ё','е').split())

LOOKUP={species_key(ru):(group,ru,en) for group,rows in GROUPS.items() for ru,en in rows}
LOOKUP['бобр']=LOOKUP['бобер']

def select_species(recent):
    # Exclude exact species from the last 20 unique recent records; additionally
    # alternate animal groups, avoiding those represented by the last three.
    keys=[species_key(row.get('species','')) for row in recent[:20]]
    seen={LOOKUP[k][1] for k in keys if k in LOOKUP}
    groups=[LOOKUP[k][0] for k in keys if k in LOOKUP]
    blocked=set(groups[:3])
    available={g:[(ru,en) for ru,en in rows if ru not in seen]
               for g,rows in GROUPS.items()}
    eligible=[g for g,rows in available.items() if rows and g not in blocked]
    if not eligible:
        eligible=[g for g,rows in available.items() if rows]
    if not eligible:
        available=GROUPS
        eligible=list(GROUPS)
    counts=Counter(groups)
    least=min(counts[g] for g in eligible)
    group=choice([g for g in eligible if counts[g]==least])
    ru,en=choice(available[group])
    return {'ru':ru,'en':en,'group':group}
