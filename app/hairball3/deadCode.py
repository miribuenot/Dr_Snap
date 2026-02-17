import logging
import app.consts_drscratch as consts
from app.hairball3.plugin import Plugin

logger = logging.getLogger(__name__)

class DeadCode(Plugin):
    """
    Plugin that identifies unreachable code in Snap! projects.
    Unreachable code is defined as any script that does NOT start with a Hat block.
    """
    def __init__(self, filename, json_project, verbose=False):
        super().__init__(filename, json_project, verbose)
        self.dead_code_instances = 0
        self.dict_deadcode = {}

    def analyze(self):
        sprites = {}
        
        for sprite_name, data in self.json_project.items():
            if not isinstance(data, dict) or 'blocks' not in data:
                continue
                
            blocks_list = data['blocks']
            dead_scripts_in_sprite = []
            
            all_children = set()
            for b in blocks_list:
                for child_id in b.get('next', []):
                    all_children.add(child_id)
                    
            root_blocks = [b for b in blocks_list if b['id'] not in all_children]
            
            for root in root_blocks:
                block_name = root.get('block', '')
                
                if not block_name.startswith('receive') and block_name not in consts.PLUGIN_DEADCODE_LIST_EVENT_VARS:
                    dead_scripts_in_sprite.append(block_name)
            
            if dead_scripts_in_sprite:
                sprites[sprite_name] = dead_scripts_in_sprite
                self.dead_code_instances += len(dead_scripts_in_sprite)

        self.dict_deadcode = sprites
        return self.dict_deadcode

    def finalize(self) -> dict:
        self.analyze()

        result = "{}".format(self.filename)
        if self.dead_code_instances > 0:
            result += "\n"
            result += str(self.dict_deadcode)

        self.dict_mastery['description'] = result
        self.dict_mastery['total_dead_code_scripts'] = self.dead_code_instances
        self.dict_mastery['list_dead_code_scripts'] = [self.dict_deadcode]

        dict_result = {'plugin': 'dead_code', 'result': self.dict_mastery}
        return dict_result