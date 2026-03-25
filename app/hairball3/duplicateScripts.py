from app.hairball3.plugin import Plugin
import logging
import coloredlogs

logger = logging.getLogger(__name__)
coloredlogs.install(level='DEBUG', logger=logger)

class DuplicateScripts(Plugin):
    """
    Plugin that analyzes duplicate scripts in Snap! projects.
    Adapted from Scratch 3.0 to process Snap! XML parsed dictionaries.
    """

    def __init__(self, filename, json_project, verbose=False):
        super().__init__(filename, json_project, verbose)
        self.total_duplicate = 0
        self.duplicates = {}
        self.list_duplicate = []
        self.list_csv = []

    def extract_scripts(self):
        """
        Recorre el diccionario generado por analyzer.py y reconstruye 
        las secuencias de bloques desde la raíz hasta el final.
        """
        all_scripts = []
        
        for sprite_name, data in self.json_project.items():
            if not isinstance(data, dict) or 'blocks' not in data:
                continue
            
            blocks_list = data['blocks']
            blocks_by_id = {b['id']: b for b in blocks_list}
            
            all_children = set()
            for b in blocks_list:
                for child_id in b.get('next', []):
                    all_children.add(child_id)
            
            root_blocks = [b for b in blocks_list if b['id'] not in all_children]
            
            for root in root_blocks:
                script_sequence = self.traverse_script(root['id'], blocks_by_id)
                
                if len(script_sequence) >= 5:
                    all_scripts.append((sprite_name, tuple(script_sequence)))
        
        return all_scripts

    def traverse_script(self, block_id, blocks_by_id):
        """
        Función recursiva que extrae los nombres de los bloques encadenados en orden.
        """
        if block_id not in blocks_by_id:
            return []
        
        block = blocks_by_id[block_id]
        seq = [block.get('block', 'unknown_block')]
        
        for child_id in block.get('next', []):
            seq.extend(self.traverse_script(child_id, blocks_by_id))
        
        return seq

    def analyze(self):
        """
        Busca scripts idénticos entre todos los objetos del proyecto.
        """
        all_scripts = self.extract_scripts()
        script_counts = {}
        
        for sprite, script_tuple in all_scripts:
            if script_tuple not in script_counts:
                script_counts[script_tuple] = []
            script_counts[script_tuple].append(sprite)
        
        for script_tuple, locations in script_counts.items():
            if len(locations) > 1:
                self.duplicates[script_tuple] = locations
                self.total_duplicate += len(locations)
                
                script_text = " -> ".join(script_tuple)
                sprites_info = ", ".join(locations)
                
                salida_legible = f"Encontrado en [{sprites_info}]: {script_text}"
                
                self.list_duplicate.append(salida_legible)
                self.list_csv.append(script_text)

        return self.duplicates

    def finalize(self) -> dict:
        
        self.analyze()

        result = ("%d duplicate scripts found" % self.total_duplicate)
        
        self.dict_mastery['description'] = result
        self.dict_mastery['total_duplicate_scripts'] = self.total_duplicate
        self.dict_mastery['list_duplicate_scripts'] = self.list_duplicate
        self.dict_mastery['duplicates'] = self.duplicates
        self.dict_mastery['list_csv'] = self.list_csv

        if self.verbose:
            logger.info(self.dict_mastery['description'])

        return {'plugin': 'duplicate_scripts', 'result': self.dict_mastery}