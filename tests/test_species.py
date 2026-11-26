import unittest
from species import select_species, GROUPS, species_key

class SpeciesTests(unittest.TestCase):
    def test_catalogue_and_recent_exclusion(self):
        self.assertGreaterEqual(sum(len(rows) for rows in GROUPS.values()),60)
        recent=[{'species':'Бобер'},{'species':'Выдра'},{'species':'Нутрия'}]
        for _ in range(50):
            selected=select_species(recent)
            self.assertNotIn(selected['group'],('rodents','mustelids'))

    def test_sequence_variety(self):
        recent=[]
        for _ in range(100):
            selected=select_species(recent)
            self.assertNotIn(species_key(selected['ru']),[species_key(r['species']) for r in recent[:20]])
            recent.insert(0,{'species':selected['ru']})
