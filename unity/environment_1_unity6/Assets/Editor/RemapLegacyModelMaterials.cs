using System.IO;
using UnityEditor;
using UnityEngine;

/// Unity 6 no longer resolves the legacy "search the Materials folder by name" import mode, so
/// models imported that way (most of the FoodAndGrocery pack) came up with blank embedded
/// materials. This writes an explicit remap from each embedded material to the .mat with the
/// same name in the model's Materials folder, which is what the legacy mode used to find.
///
/// Batch: Unity -batchmode -quit -projectPath . -executeMethod RemapLegacyModelMaterials.Run
public static class RemapLegacyModelMaterials
{
    [MenuItem("BCI/Remap Legacy Model Materials")]
    public static void Run()
    {
        int models = 0, remaps = 0;
        foreach (var guid in AssetDatabase.FindAssets("t:Model", new[] { "Assets" }))
        {
            string path = AssetDatabase.GUIDToAssetPath(guid);
            if (!(AssetImporter.GetAtPath(path) is ModelImporter importer)) continue;

            string dir = Path.GetDirectoryName(path);
            var existing = importer.GetExternalObjectMap();
            bool changed = false;
            foreach (var obj in AssetDatabase.LoadAllAssetsAtPath(path))
            {
                if (!(obj is Material embedded)) continue;
                var id = new AssetImporter.SourceAssetIdentifier(typeof(Material), embedded.name);
                if (existing.ContainsKey(id)) continue;
                var external = FindMaterial(dir, embedded.name);
                if (external == null) continue;
                importer.AddRemap(id, external);
                changed = true;
                remaps++;
            }
            if (changed)
            {
                importer.SaveAndReimport();
                models++;
            }
        }
        Debug.Log("[RemapLegacyModelMaterials] Remapped " + remaps + " materials on " + models + " models.");
    }

    /// Looks where the legacy importer looked: a Materials folder next to the model, then up the tree.
    static Material FindMaterial(string dir, string name)
    {
        while (!string.IsNullOrEmpty(dir) && dir.StartsWith("Assets"))
        {
            string candidate = Path.Combine(dir, "Materials", name + ".mat").Replace('\\', '/');
            var m = AssetDatabase.LoadAssetAtPath<Material>(candidate);
            if (m != null) return m;
            dir = Path.GetDirectoryName(dir);
        }
        return null;
    }
}
