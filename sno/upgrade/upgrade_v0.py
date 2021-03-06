import functools
import json
import re

from sno import gpkg_adapter
from sno.geometry import normalise_gpkg_geom
from sno.base_dataset import BaseDataset


class UpgradeDataset0(BaseDataset):
    """A V0 dataset / import source, only used for upgrading to V2 and beyond."""

    VERSION = 0

    META_PATH = "meta/"
    FEATURE_PATH = "features/"

    @classmethod
    def is_dataset_tree(cls, tree):
        if "meta" in tree and (tree / "meta").type_str == "tree":
            meta_tree = tree / "meta"
            return "version" in meta_tree and (meta_tree / "version").type_str == "blob"
        return False

    def _iter_feature_dirs(self):
        """
        Iterates over all the features in self.tree that match the expected
        pattern for a feature, and yields the following for each:
        >>> feature_builder(path_name, path_data)
        """
        if self.FEATURE_PATH not in self.tree:
            return

        RE_DIR1 = re.compile(r"([0-9a-f]{4})?$")
        RE_DIR2 = re.compile(r"([0-9a-f-]{36})?$")

        for dir1 in self.feature_tree:
            if hasattr(dir1, "data") or not RE_DIR1.match(dir1.name):
                continue

            for dir2 in dir1:
                if hasattr(dir2, "data") or not RE_DIR2.match(dir2.name):
                    continue

                yield dir2

    @functools.lru_cache()
    def get_meta_item(self, name):
        return gpkg_adapter.generate_v2_meta_item(self, name)

    @functools.lru_cache()
    def get_gpkg_meta_item(self, name):
        rel_path = self.META_PATH + name
        data = self.get_data_at(
            rel_path, missing_ok=(name in gpkg_adapter.GPKG_META_ITEM_NAMES)
        )
        # For V0 / V1, all data is serialised using json.dumps
        return json.loads(data) if data is not None else None

    def crs_definitions(self):
        return gpkg_adapter.all_v2_crs_definitions(self)

    def features(self):
        ggc = self.get_gpkg_meta_item("gpkg_geometry_columns")
        geom_field = ggc["column_name"] if ggc else None

        for feature_dir in self._iter_feature_dirs():
            source_feature_dict = {}
            for attr_blob in feature_dir:
                if not hasattr(attr_blob, "data"):
                    continue
                attr = attr_blob.name
                if attr == geom_field:
                    source_feature_dict[attr] = normalise_gpkg_geom(attr_blob.data)
                else:
                    source_feature_dict[attr] = json.loads(
                        attr_blob.data.decode("utf8")
                    )
            yield source_feature_dict

    @property
    def feature_count(self):
        count = 0
        for feature_dirs in self._iter_feature_dirs():
            count += 1
        return count
