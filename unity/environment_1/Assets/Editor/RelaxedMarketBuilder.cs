using System.Collections.Generic;
using System.IO;
using TMPro;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

/// Builds the "Sunday Morning Market" decor for environment 1 (FreeNavScene).
///
/// Everything this script adds lives under the root object "_RelaxedMarket" and is deleted and
/// rebuilt on every run, so the layout below is the source of truth. It also restyles the existing
/// level kit (warm wall tint, softer light strips, still ceiling fans) and the scene lighting.
/// It never moves or renames the existing shelves and food items, because ObjectPicker and the
/// voice prompts depend on them.
///
/// Menu: BCI > Rebuild Relaxed Market.
/// Batch: Unity -batchmode -quit -projectPath . -executeMethod RelaxedMarketBuilder.BuildAndSave
public static class RelaxedMarketBuilder
{
    const string ScenePath = "Assets/Scenes/FreeNavScene.unity";
    const string RootName = "_RelaxedMarket";
    const string AssetDir = "Assets/RelaxedMarket";
    const string FoodDir = "Assets/FoodAndGrocery/Items/";

    // Auto-move data collection route (AutoMoveDataCollectionController sequence from the spawn).
    // Props keep at least RouteClearance metres from it so that sequence still runs unobstructed.
    static readonly Vector2[] Route =
    {
        new Vector2(0, 0), new Vector2(0, -6), new Vector2(-12, -6), new Vector2(-12, 3), new Vector2(-15, 3),
    };
    const float RouteClearance = 0.8f;

    static Transform root;
    static System.Random rng;
    static readonly Dictionary<string, Material> mats = new Dictionary<string, Material>();
    static readonly List<string> warnings = new List<string>();

    [MenuItem("BCI/Rebuild Relaxed Market")]
    public static void BuildInOpenScene()
    {
        Build();
        EditorSceneManager.MarkSceneDirty(UnityEngine.SceneManagement.SceneManager.GetActiveScene());
    }

    public static void BuildAndSave()
    {
        var scene = EditorSceneManager.OpenScene(ScenePath);
        Build();
        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene);
        AssetDatabase.SaveAssets();
    }

    static void Build()
    {
        rng = new System.Random(20261108);
        mats.Clear();
        warnings.Clear();

        var old = GameObject.Find(RootName);
        if (old != null) Object.DestroyImmediate(old);
        root = new GameObject(RootName).transform;

        CreateMaterials();
        RestyleLevel();
        SetUpLighting();

        BuildFloors();
        BuildProduceHall();
        BuildBakery();
        BuildDairy();
        BuildPantry();
        BuildCafeWing();
        BuildLandmarksAndGreenery();

        FinalizeColliders();
        CheckPlacement();

        foreach (var w in warnings) Debug.LogWarning("[RelaxedMarket] " + w);
        Debug.Log("[RelaxedMarket] Built " + root.GetComponentsInChildren<Transform>().Length + " objects, " + warnings.Count + " warnings.");
    }

    // ------------------------------------------------------------------ materials

    static void CreateMaterials()
    {
        Directory.CreateDirectory(AssetDir + "/Materials");
        Directory.CreateDirectory(AssetDir + "/Textures");

        var planks = MakePlankTexture(AssetDir + "/Textures/WoodPlanks.png");

        Mat("FloorWood", new Color(0.86f, 0.70f, 0.52f), 0.18f, planks, new Vector2(1f, 1f));
        Mat("Oak", new Color(0.72f, 0.53f, 0.34f), 0.2f);
        Mat("OakLight", new Color(0.85f, 0.70f, 0.50f), 0.2f);
        Mat("Walnut", new Color(0.36f, 0.24f, 0.16f), 0.25f);
        Mat("CrateWood", new Color(0.80f, 0.62f, 0.42f), 0.1f);
        Mat("Terracotta", new Color(0.76f, 0.43f, 0.30f), 0.1f);
        Mat("Soil", new Color(0.24f, 0.17f, 0.12f), 0.0f);
        Mat("LeafDark", new Color(0.24f, 0.42f, 0.24f), 0.15f);
        Mat("Leaf", new Color(0.36f, 0.55f, 0.30f), 0.15f);
        Mat("LeafLight", new Color(0.52f, 0.67f, 0.36f), 0.15f);
        Mat("Sage", new Color(0.62f, 0.70f, 0.58f), 0.05f);
        Mat("Cream", new Color(0.95f, 0.91f, 0.82f), 0.2f);
        Mat("Linen", new Color(0.90f, 0.85f, 0.76f), 0.05f);
        Mat("Marble", new Color(0.93f, 0.91f, 0.88f), 0.55f);
        Mat("Brass", new Color(0.78f, 0.62f, 0.36f), 0.6f, null, Vector2.one, 0.6f);
        Mat("Charcoal", new Color(0.20f, 0.20f, 0.19f), 0.3f);
        Mat("Chalk", new Color(0.17f, 0.22f, 0.20f), 0.05f);
        Mat("RugTerracotta", new Color(0.70f, 0.45f, 0.35f), 0.0f);
        Mat("RugSage", new Color(0.55f, 0.62f, 0.50f), 0.0f);
        Mat("CoolerWhite", new Color(0.88f, 0.92f, 0.93f), 0.45f);
        Mat("CoolerInside", new Color(0.55f, 0.62f, 0.66f), 0.3f);
        Mat("CartMetal", new Color(0.55f, 0.57f, 0.58f), 0.5f, null, Vector2.one, 0.5f);
        Mat("Rubber", new Color(0.12f, 0.12f, 0.12f), 0.1f);
        Mat("FlowerPink", new Color(0.93f, 0.62f, 0.68f), 0.1f);
        Mat("FlowerYellow", new Color(0.97f, 0.84f, 0.45f), 0.1f);
        Mat("FlowerWhite", new Color(0.97f, 0.95f, 0.90f), 0.1f);
        Mat("FlowerLavender", new Color(0.70f, 0.62f, 0.85f), 0.1f);
        Mat("Stem", new Color(0.35f, 0.50f, 0.28f), 0.1f);

        Emissive("LampGlow", new Color(1f, 0.86f, 0.62f), 1.4f);
        Emissive("CoolStrip", new Color(0.85f, 0.95f, 1f), 0.9f);

        // Warm copies of the level kit materials. The kit assets themselves stay untouched.
        var kitDiffuse = AssetDatabase.LoadAssetAtPath<Material>("Assets/_Barking_Dog/3D Free Modular Kit/_Meshes/Materials/Diffuse_01.mat");
        var kitNeon = AssetDatabase.LoadAssetAtPath<Material>("Assets/_Barking_Dog/3D Free Modular Kit/_Meshes/Materials/Neon_01.mat");
        if (kitDiffuse != null)
        {
            var m = CopyMat(kitDiffuse, "KitWarm");
            m.SetColor("_Color", new Color(1.0f, 0.93f, 0.84f));
            m.SetFloat("_Metallic", 0.03f);
            m.SetFloat("_GlossMapScale", 0.35f);
            m.SetFloat("_Glossiness", 0.22f);
        }
        else warnings.Add("Kit Diffuse_01 material not found; walls keep their original look.");
        if (kitNeon != null)
        {
            var m = CopyMat(kitNeon, "KitNeonSoft");
            m.SetColor("_Color", new Color(1f, 0.86f, 0.66f));
            m.SetColor("_EmissionColor", new Color(0.42f, 0.32f, 0.22f));
        }
    }

    static Material Mat(string name, Color c, float gloss, Texture2D tex = null, Vector2 tiling = default, float metallic = 0f)
    {
        string path = AssetDir + "/Materials/" + name + ".mat";
        var m = AssetDatabase.LoadAssetAtPath<Material>(path);
        if (m == null)
        {
            m = new Material(Shader.Find("Standard"));
            AssetDatabase.CreateAsset(m, path);
        }
        m.SetColor("_Color", c);
        m.SetFloat("_Glossiness", gloss);
        m.SetFloat("_Metallic", metallic);
        m.mainTexture = tex;
        if (tex != null) m.mainTextureScale = tiling == default ? Vector2.one : tiling;
        m.enableInstancing = true;
        EditorUtility.SetDirty(m);
        mats[name] = m;
        return m;
    }

    static Material Emissive(string name, Color c, float intensity)
    {
        var m = Mat(name, c, 0.1f);
        m.EnableKeyword("_EMISSION");
        m.globalIlluminationFlags = MaterialGlobalIlluminationFlags.None;
        m.SetColor("_EmissionColor", c * intensity);
        EditorUtility.SetDirty(m);
        return m;
    }

    static Material CopyMat(Material src, string name)
    {
        string path = AssetDir + "/Materials/" + name + ".mat";
        var m = AssetDatabase.LoadAssetAtPath<Material>(path);
        if (m == null)
        {
            m = new Material(src);
            AssetDatabase.CreateAsset(m, path);
        }
        else m.CopyPropertiesFromMaterial(src);
        EditorUtility.SetDirty(m);
        mats[name] = m;
        return m;
    }

    /// Writes a tileable 2 m x 2 m light-oak plank texture (8 planks, staggered joints, soft grain).
    static Texture2D MakePlankTexture(string path)
    {
        const int size = 512;
        var tex = new Texture2D(size, size, TextureFormat.RGB24, false);
        var prng = new System.Random(7);
        int planks = 8, plankW = size / planks;
        float[] tone = new float[planks];
        int[] joint = new int[planks];
        for (int i = 0; i < planks; i++) { tone[i] = 0.9f + (float)prng.NextDouble() * 0.16f; joint[i] = prng.Next(size); }
        for (int y = 0; y < size; y++)
        for (int x = 0; x < size; x++)
        {
            int p = x / plankW;
            float t = tone[p];
            float grain = 0.04f * Mathf.Sin((y * 0.09f) + Mathf.Sin(x * 0.25f + p * 3f) * 2.2f) + 0.025f * Mathf.PerlinNoise(x * 0.05f + p * 10f, y * 0.012f);
            float v = t + grain;
            int lx = x % plankW;
            if (lx == 0 || lx == plankW - 1) v *= 0.72f;                // seam between planks
            if (Mathf.Abs(y - joint[p]) < 2 || Mathf.Abs(y - (joint[p] + size / 2) % size) < 2) v *= 0.78f; // butt joints
            tex.SetPixel(x, y, new Color(v, v, v));
        }
        tex.Apply();
        File.WriteAllBytes(path, tex.EncodeToPNG());
        AssetDatabase.ImportAsset(path);
        var importer = (TextureImporter)AssetImporter.GetAtPath(path);
        importer.wrapMode = TextureWrapMode.Repeat;
        importer.anisoLevel = 4;
        importer.SaveAndReimport();
        return AssetDatabase.LoadAssetAtPath<Texture2D>(path);
    }

    // ------------------------------------------------------------------ level restyle + light

    static void RestyleLevel()
    {
        var level = GameObject.Find("_Level");
        if (level == null) { warnings.Add("_Level not found; skipped wall restyle."); return; }

        mats.TryGetValue("KitWarm", out var warm);
        mats.TryGetValue("KitNeonSoft", out var neon);
        foreach (var r in level.GetComponentsInChildren<Renderer>(true))
        {
            var shared = r.sharedMaterials;
            bool changed = false;
            for (int i = 0; i < shared.Length; i++)
            {
                if (shared[i] == null) continue;
                if (warm != null && shared[i].name == "Diffuse_01") { shared[i] = warm; changed = true; }
                else if (neon != null && shared[i].name == "Neon_01") { shared[i] = neon; changed = true; }
            }
            if (changed) r.sharedMaterials = shared;
        }

        // Ceiling fans spin through Turn_Move. A rotating object in view adds periodic visual
        // motion to the EEG, so the fans stay still.
        foreach (var mb in level.GetComponentsInChildren<MonoBehaviour>(true))
            if (mb != null && mb.GetType().Name == "Turn_Move") mb.enabled = false;

        foreach (var go in level.scene.GetRootGameObjects())
        {
            // The 1 m XR grab test cube sits in the middle of the view from the spawn and hides
            // the produce island. No script references it, so it is only deactivated.
            if (go.name == "Cube" && go.GetComponent<MeshRenderer>() != null)
                go.SetActive(false);

            // The original black metal shelves get the oak finish of the new furniture. Only the
            // material changes; the shelves and their named food items stay where they are.
            if (go.name == "shelf")
                foreach (var r in go.GetComponentsInChildren<MeshRenderer>(true))
                    if (r.sharedMaterial != null && r.sharedMaterial.name == "shelf")
                        r.sharedMaterial = mats["Oak"];
        }
    }

    static void SetUpLighting()
    {
        var sun = GameObject.Find("Directional Light");
        if (sun != null)
        {
            var l = sun.GetComponent<Light>();
            l.color = new Color(1f, 0.89f, 0.74f);
            l.intensity = 0.45f;
            l.shadows = LightShadows.Soft;
            l.shadowStrength = 0.35f;
            sun.transform.rotation = Quaternion.Euler(78f, 330f, 0f);
        }

        RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Trilight;
        RenderSettings.ambientSkyColor = new Color(0.66f, 0.60f, 0.53f);
        RenderSettings.ambientEquatorColor = new Color(0.55f, 0.48f, 0.41f);
        RenderSettings.ambientGroundColor = new Color(0.33f, 0.28f, 0.23f);
        // The default sky reflection tints the ceiling blue; indoors there is no sky to reflect.
        RenderSettings.defaultReflectionMode = UnityEngine.Rendering.DefaultReflectionMode.Custom;
        RenderSettings.customReflection = null;
        RenderSettings.reflectionIntensity = 0.3f;
        RenderSettings.fog = false;

        // Warm, shadowless zone lights. Unshadowed point lights stay cheap on Quest.
        var lights = Group("ZoneLights");
        Vector3[] spots =
        {
            new Vector3(0f, 2.8f, 1.6f), new Vector3(0f, 2.8f, 5.4f),          // produce hall
            new Vector3(1.6f, 2.8f, -3f), new Vector3(-3.5f, 2.8f, -7f),        // bakery, dairy
            new Vector3(-10.4f, 2.8f, -3f), new Vector3(-13.5f, 2.8f, -7.5f),   // pantry
            new Vector3(-15.5f, 2.8f, 4.6f), new Vector3(-9.8f, 2.8f, 4.4f),    // cafe wing
        };
        for (int i = 0; i < spots.Length; i++)
            PointLight(lights, "ZoneLight_" + i, spots[i], new Color(1f, 0.80f, 0.60f), 0.9f, 6.5f);
    }

    static Light PointLight(Transform parent, string name, Vector3 pos, Color c, float intensity, float range)
    {
        var go = new GameObject(name);
        go.transform.SetParent(parent, false);
        go.transform.position = pos;
        var l = go.AddComponent<Light>();
        l.type = LightType.Point;
        l.color = c;
        l.intensity = intensity;
        l.range = range;
        l.shadows = LightShadows.None;
        l.renderMode = LightRenderMode.Auto;
        return l;
    }

    // ------------------------------------------------------------------ zones

    static void BuildFloors()
    {
        // Wood decking laid 5 mm above the kit floor, so the planks tile in world space.
        var g = Group("WoodFloor");
        Rect[] areas =
        {
            new Rect(-3f, 0f, 6f, 8f),      // produce hall incl. north door threshold
            new Rect(-15f, -9f, 18f, 9f),   // south hall
            new Rect(-15f, 0f, 6f, 1f),     // arches into the cafe wing
            new Rect(-18f, 1f, 12f, 6f),    // cafe wing
        };
        foreach (var a in areas)
        {
            var deck = Box(g, "Deck", new Vector3(a.center.x, 0.0025f, a.center.y), new Vector3(a.width, 0.005f, a.height), "FloorWood");
            // Each plank texture tile covers 2 m.
            var mf = deck.GetComponent<MeshFilter>();
            var mesh = Object.Instantiate(mf.sharedMesh);
            var uvs = mesh.uv;
            for (int i = 0; i < uvs.Length; i++) uvs[i] = new Vector2(uvs[i].x * a.width / 2f, uvs[i].y * a.height / 2f);
            mesh.uv = uvs;
            mesh.name = "DeckMesh";
            mf.sharedMesh = mesh;
        }
    }

    static void BuildProduceHall()
    {
        var g = Group("ProduceHall");

        // Center island: a low oak table with tilted crates, facing the spawn.
        var island = Group("ProduceIsland", g);
        Vector3 c = new Vector3(0f, 0f, 5.3f);
        Table(island, c, new Vector3(1.1f, 0.72f, 1.7f), "Oak");
        string[][] crateFill =
        {
            new[] { "Pear/Pear" }, new[] { "Lemon/Lemon" },
            new[] { "Strawberry/Strawberry" }, new[] { "Carrot/Carrot" },
            new[] { "Cucumber/Cucumber" }, new[] { "Mushroom/Mushroom" },
        };
        int k = 0;
        for (int row = 0; row < 3; row++)
        for (int col = 0; col < 2; col++)
        {
            var pos = c + new Vector3(col == 0 ? -0.27f : 0.27f, 0.72f, -0.55f + row * 0.55f);
            ProduceCrate(island, pos, new Vector3(0.5f, 0.14f, 0.48f), crateFill[k++], col == 0 ? 8f : -8f);
        }
        // Leeks standing in a basket at the north end of the island.
        var crock = Cylinder(island, "Crock", c + new Vector3(0f, 0.83f, 0.95f), new Vector3(0.2f, 0.11f, 0.2f), "Terracotta");
        FillCircle(island, FoodDir + "Leek/Leek.fbx", crock.position.y + 0.06f, crock.position, 0.1f, 5, 0.32f, true);

        HangingSign(g, "Fresh Produce", new Vector3(0f, 2.55f, 4.3f), 0f);
        PendantLamp(g, c + new Vector3(0f, 0f, -0.45f), 2.15f, false);
        PendantLamp(g, c + new Vector3(0f, 0f, 0.45f), 2.15f, false);

        PottedPlant(g, new Vector3(-2.0f, 0f, 6.55f), 1.3f);
        PottedPlant(g, new Vector3(2.0f, 0f, 6.55f), 1.3f);
        PottedPlant(g, new Vector3(-2.6f, 0f, 0.3f), 0.9f);
    }

    static void BuildBakery()
    {
        var g = Group("Bakery");

        // Counter along the east wall, opening toward the spawn corridor.
        var counter = Group("BakeryCounter", g);
        Box(counter, "Body", new Vector3(2.5f, 0.45f, -3.0f), new Vector3(0.75f, 0.9f, 3.0f), "Oak");
        Box(counter, "Top", new Vector3(2.47f, 0.92f, -3.0f), new Vector3(0.85f, 0.04f, 3.1f), "Marble");
        Box(counter, "Kick", new Vector3(2.11f, 0.05f, -3.0f), new Vector3(0.02f, 0.1f, 2.96f), "Walnut");
        for (int i = 0; i < 4; i++)
        {
            float z = -4.2f + i * 0.8f;
            Box(counter, "Board", new Vector3(2.45f, 0.95f, z), new Vector3(0.42f, 0.025f, 0.6f), "Walnut");
            if (i % 2 == 0) Row(counter, FoodDir + "Bread/Bread.fbx", new Vector3(2.45f, 0.963f, z), 0.26f, 2, 0.28f, 90f, Vector3.forward);
            else Row(counter, FoodDir + "Sandwich/Sandwich.fbx", new Vector3(2.45f, 0.963f, z), 0.13f, 3, 0.17f, 90f, Vector3.forward);
        }
        HangingSign(g, "Bakery", new Vector3(1.9f, 2.55f, -3.0f), 90f);
        PendantLamp(g, new Vector3(2.35f, 0f, -3.9f), 2.05f, false);
        PendantLamp(g, new Vector3(2.35f, 0f, -2.1f), 2.05f, false);

        // Bread crates stacked near the rounded south-east corner.
        CrateStack(g, new Vector3(2.2f, 0f, -6.6f), FoodDir + "Bread/Bread.fbx", 0.24f);
        PottedPlant(g, new Vector3(2.45f, 0f, -0.4f), 1.4f);
    }

    static void BuildDairy()
    {
        var g = Group("Dairy");
        // Low open cooler continuing the existing milk shelves along the south wall.
        var cooler = Group("Cooler", g);
        Vector3 c = new Vector3(-4.65f, 0f, -8.6f);
        float len = 2.4f;
        Box(cooler, "Body", c + new Vector3(0, 0.4f, 0), new Vector3(len, 0.8f, 0.7f), "CoolerWhite");
        Box(cooler, "Well", c + new Vector3(0, 0.801f, 0.02f), new Vector3(len - 0.12f, 0.004f, 0.56f), "CoolerInside");
        Box(cooler, "Back", c + new Vector3(0, 1.15f, -0.3f), new Vector3(len, 0.7f, 0.1f), "CoolerWhite");
        Box(cooler, "Shelf", c + new Vector3(0, 1.13f, -0.15f), new Vector3(len, 0.02f, 0.22f), "CoolerWhite");
        Box(cooler, "Strip", c + new Vector3(0, 1.49f, -0.26f), new Vector3(len - 0.1f, 0.025f, 0.03f), "CoolStrip");
        // Well: rows of yogurt, cheese, mozzarella; back shelf: milk.
        string[] well = { "Joghurt/Prefabs/Joghurt_Blue.prefab", "Joghurt/Prefabs/Joghurt_Yellow.prefab", "Cheese/Prefabs/Cheese_White.prefab", "Cheese/Prefabs/Cheese_Dark.prefab", "Mozzarella/Mozzarella.fbx", "Joghurt/Prefabs/Joghurt_Green.prefab" };
        for (int i = 0; i < well.Length; i++)
        {
            float x = c.x - len / 2 + 0.25f + i * ((len - 0.5f) / (well.Length - 1));
            Grid(cooler, FoodDir + well[i], new Vector3(x, 0.803f, c.z + 0.02f), 0.09f, 2, 4, 0.13f, 0.12f);
        }
        Row(cooler, FoodDir + "Milk/Milk.fbx", new Vector3(c.x, 1.14f, c.z - 0.15f), 0.2f, 12, 0.18f, 0f, Vector3.right);
        HangingSign(g, "Dairy", new Vector3(-3.2f, 2.55f, -7.6f), 0f);
    }

    static void BuildPantry()
    {
        var g = Group("Pantry");
        string[] dry = { "Can/Prefabs/Can_Red.prefab", "Can/Prefabs/Can_Green.prefab", "Can/Prefabs/Can_Orange.prefab", "Can/Prefabs/Can_Black.prefab" };
        string[] snacks = { "Chips/Prefabs/Chips_Red.prefab", "Chips/Prefabs/Chips_Green.prefab", "Chips/Prefabs/Chips_White.prefab", "Dough/Prefabs/Dough_Blue.prefab", "Dough/Prefabs/Dough_Green.prefab" };
        string[] drinks = { "PetBottles/Prefabs/Bottle_Water.prefab", "BeerBox/Prefabs/BeerBox_Green.prefab", "BeerBox/Prefabs/BeerBox_Blue.prefab", "PetBottles/Prefabs/Bottle_Water.prefab" };
        string[] home = { "SoapBottle/SoapBottle.fbx", "SprayBottle/SprayBottle.fbx", "SoapBottle/SoapBottle.fbx", "Can/Prefabs/Can_Green.prefab" };

        // Double-sided gondola between the route and the rounded pillar block.
        ShelfUnit(g, new Vector3(-10.1f, 0f, -2.0f), 270f, 2.4f, new[] { snacks, dry, snacks, drinks });
        ShelfUnit(g, new Vector3(-9.66f, 0f, -2.0f), 90f, 2.4f, new[] { drinks, home, dry, snacks });
        // Wall shelving along the west wall.
        ShelfUnit(g, new Vector3(-14.72f, 0f, -3.6f), 90f, 2.4f, new[] { dry, snacks, drinks, home });
        // Low shelf on the south wall west of the round niche.
        ShelfUnit(g, new Vector3(-10.2f, 0f, -8.72f), 0f, 1.6f, new[] { drinks, dry, snacks, home });

        HangingSign(g, "Pantry", new Vector3(-10.0f, 2.55f, -3.6f), 90f);
    }

    static void BuildCafeWing()
    {
        var g = Group("CafeWing");

        // Sage accent wall with a living plant wall on the north side of the wing.
        Box(g, "AccentWall", new Vector3(-13.5f, 1.45f, 6.975f), new Vector3(9f, 2.9f, 0.03f), "Sage");
        LivingWall(g, new Vector3(-15.5f, 1.55f, 6.93f), 3.2f, 1.6f);

        // Cafe tables along the north side, clear of the route that crosses the wing at z = 3.
        Rug(g, new Vector3(-15.6f, 0f, 5.55f), new Vector2(3.6f, 2.3f), "RugTerracotta");
        CafeTable(g, new Vector3(-16.6f, 0f, 5.5f));
        CafeTable(g, new Vector3(-14.4f, 0f, 5.6f));
        Rug(g, new Vector3(-10.4f, 0f, 5.45f), new Vector2(2.6f, 2.2f), "RugSage");
        CafeTable(g, new Vector3(-10.4f, 0f, 5.4f));
        PendantLamp(g, new Vector3(-16.6f, 0f, 5.5f), 2.0f, true);
        PendantLamp(g, new Vector3(-14.4f, 0f, 5.6f), 2.0f, true);
        PendantLamp(g, new Vector3(-10.4f, 0f, 5.4f), 2.0f, true);

        // Small coffee counter between the two seating areas.
        var counter = Group("CoffeeCounter", g);
        Box(counter, "Body", new Vector3(-12.6f, 0.47f, 6.55f), new Vector3(1.6f, 0.94f, 0.6f), "OakLight");
        Box(counter, "Top", new Vector3(-12.6f, 0.955f, 6.53f), new Vector3(1.7f, 0.03f, 0.68f), "Walnut");
        Prefab(counter, FoodDir + "SaladBowl/SaladBowl.fbx", new Vector3(-13.1f, 0.97f, 6.5f), 0f, 0.26f);
        Prefab(counter, FoodDir + "MenuBowl/MenuBowl.fbx", new Vector3(-12.4f, 0.97f, 6.5f), 0f, 0.22f);
        Row(counter, FoodDir + "Apple/Apple.fbx", new Vector3(-12.4f, 1.0f, 6.5f), 0.075f, 3, 0.08f, 0f, Vector3.right);
        Chalkboard(g, new Vector3(-12.6f, 1.75f, 6.93f), "Cafe\n<size=60%>tea   coffee   juice</size>");

        PottedPlant(g, new Vector3(-17.55f, 0f, 1.45f), 1.5f);
        PottedPlant(g, new Vector3(-9.4f, 0f, 1.5f), 1.0f);
        HangingSign(g, "Cafe", new Vector3(-12f, 2.55f, 1.6f), 0f);
    }

    static void BuildLandmarksAndGreenery()
    {
        var g = Group("Landmarks");
        // Landmark trees at the ends of the long sight lines.
        PottedTree(g, new Vector3(-14.05f, 0f, -1.0f), 2.6f);  // west wall, at the end of the pantry aisle
        PottedTree(g, new Vector3(-7.6f, 0f, 4.0f), 2.5f);     // rounded east end of the cafe wing
        // Flower cart beside the spawn, visible on the first turn.
        FlowerCart(g, new Vector3(-1.95f, 0f, -1.7f), 90f);
        // Shopping carts parked by the bakery.
        Cart(g, new Vector3(2.3f, 0f, -5.4f), 180f);
        Cart(g, new Vector3(2.3f, 0f, -5.0f), 180f);
        // Planter boxes along the curved pillar block.
        PlanterBox(g, new Vector3(-5.3f, 0f, -3.3f), 2.2f);
        PottedPlant(g, new Vector3(-14.6f, 0f, -8.3f), 1.2f);
        Bench(g, new Vector3(-14.45f, 0f, -6.3f), 90f);
        PottedPlant(g, new Vector3(-20f, 0f, 1.0f), 1.4f);
        PottedPlant(g, new Vector3(-20f, 0f, 6.5f), 1.4f);
    }

    // ------------------------------------------------------------------ props

    static void Table(Transform parent, Vector3 center, Vector3 size, string mat)
    {
        var t = Group("Table", parent);
        Box(t, "Top", center + new Vector3(0, size.y - 0.03f, 0), new Vector3(size.x, 0.06f, size.z), mat);
        float lx = size.x / 2 - 0.07f, lz = size.z / 2 - 0.07f;
        foreach (var s in new[] { new Vector2(-1, -1), new Vector2(1, -1), new Vector2(-1, 1), new Vector2(1, 1) })
            Box(t, "Leg", center + new Vector3(s.x * lx, (size.y - 0.06f) / 2, s.y * lz), new Vector3(0.07f, size.y - 0.06f, 0.07f), mat);
        Box(t, "Shelf", center + new Vector3(0, 0.18f, 0), new Vector3(size.x - 0.1f, 0.03f, size.z - 0.1f), "Walnut");
    }

    /// Open wooden crate tilted toward the viewer, heaped with one kind of produce.
    static void ProduceCrate(Transform parent, Vector3 basePos, Vector3 size, string[] items, float tiltDeg)
    {
        var crate = Group("Crate", parent);
        crate.position = basePos;
        crate.rotation = Quaternion.Euler(0, 0, tiltDeg);
        float w = size.x, h = size.y, d = size.z, t = 0.018f;
        BoxLocal(crate, "Bottom", new Vector3(0, t / 2, 0), new Vector3(w, t, d), "CrateWood");
        BoxLocal(crate, "SideL", new Vector3(-w / 2 + t / 2, h / 2, 0), new Vector3(t, h, d), "CrateWood");
        BoxLocal(crate, "SideR", new Vector3(w / 2 - t / 2, h / 2, 0), new Vector3(t, h, d), "CrateWood");
        BoxLocal(crate, "Front", new Vector3(0, h / 2, -d / 2 + t / 2), new Vector3(w, h, t), "Walnut");
        BoxLocal(crate, "Back", new Vector3(0, h / 2, d / 2 - t / 2), new Vector3(w, h, t), "Walnut");
        string path = FoodDir + items[0] + ".fbx";
        float target = FoodSize(items[0]);
        int nx = Mathf.Max(2, Mathf.FloorToInt((w - 0.06f) / (target * 1.05f)));
        int nz = Mathf.Max(2, Mathf.FloorToInt((d - 0.06f) / (target * 1.05f)));
        for (int layer = 0; layer < 2; layer++)
        for (int i = 0; i < nx; i++)
        for (int j = 0; j < nz; j++)
        {
            if (layer == 1 && (i == 0 || j == 0 || i == nx - 1 || j == nz - 1)) continue;
            var local = new Vector3(-w / 2 + 0.03f + (i + 0.5f) * (w - 0.06f) / nx + Jit(0.01f), t + layer * target * 0.6f, -d / 2 + 0.03f + (j + 0.5f) * (d - 0.06f) / nz + Jit(0.01f));
            var item = Food(crate, path, target, crate.TransformPoint(local), (float)rng.NextDouble() * 360f);
            if (item != null) SitOn(item, crate.TransformPoint(local).y);
        }
    }

    static void CrateStack(Transform parent, Vector3 pos, string itemPath, float itemSize)
    {
        var g = Group("CrateStack", parent);
        Box(g, "Crate1", pos + new Vector3(0, 0.15f, 0), new Vector3(0.55f, 0.3f, 0.4f), "CrateWood");
        Box(g, "Crate2", pos + new Vector3(0, 0.45f, 0), new Vector3(0.55f, 0.3f, 0.4f), "Oak");
        Row(g, itemPath, pos + new Vector3(0, 0.6f, -0.08f), itemSize, 2, 0.26f, 90f, Vector3.right);
        Row(g, itemPath, pos + new Vector3(0, 0.6f, 0.1f), itemSize, 2, 0.26f, 90f, Vector3.right);
    }

    /// Wooden shelving unit. yaw 0 means the front faces +z.
    static void ShelfUnit(Transform parent, Vector3 pos, float yaw, float length, string[][] boards)
    {
        var u = Group("ShelfUnit", parent);
        u.position = pos;
        u.rotation = Quaternion.Euler(0, yaw, 0);
        const float depth = 0.42f, height = 1.55f, t = 0.025f;
        float[] heights = { 0.12f, 0.48f, 0.84f, 1.2f };
        BoxLocal(u, "Back", new Vector3(0, height / 2, -depth / 2 + t / 2), new Vector3(length, height, t), "OakLight");
        BoxLocal(u, "SideL", new Vector3(-length / 2 + t / 2, height / 2, 0), new Vector3(t, height, depth), "Oak");
        BoxLocal(u, "SideR", new Vector3(length / 2 - t / 2, height / 2, 0), new Vector3(t, height, depth), "Oak");
        BoxLocal(u, "Cap", new Vector3(0, height, 0), new Vector3(length + 0.02f, t, depth + 0.02f), "Oak");
        BoxLocal(u, "Kick", new Vector3(0, 0.05f, 0), new Vector3(length, 0.1f, depth), "Walnut");
        for (int b = 0; b < heights.Length; b++)
        {
            BoxLocal(u, "Board", new Vector3(0, heights[b], 0), new Vector3(length - 2 * t, t, depth - t), "Oak");
            string[] palette = boards[b % boards.Length];
            // Products in colour blocks, two deep, faces toward the aisle.
            float x = -length / 2 + t + 0.03f;
            int block = 0;
            while (x < length / 2 - t - 0.05f)
            {
                string item = palette[block % palette.Length];
                float size = FoodSize(item);
                float step = size * 0.95f + 0.02f;
                int count = Mathf.Max(2, Mathf.FloorToInt(0.42f / step));
                for (int n = 0; n < count && x + step < length / 2 - t; n++, x += step)
                    for (int row = 0; row < 2; row++)
                    {
                        var local = new Vector3(x + step / 2, heights[b] + t / 2, 0.08f - row * 0.17f);
                        var go = Food(u, FoodDir + item, size, u.TransformPoint(local), yaw + 180f + Jit(6f));
                        if (go != null) SitOn(go, u.TransformPoint(local).y);
                    }
                x += 0.04f;
                block++;
            }
        }
    }

    static void PendantLamp(Transform parent, Vector3 floorPos, float shadeHeight, bool withLight)
    {
        var g = Group("PendantLamp", parent);
        const float ceiling = 3.0f;
        Cylinder(g, "Cord", new Vector3(floorPos.x, (ceiling + shadeHeight) / 2, floorPos.z), new Vector3(0.012f, (ceiling - shadeHeight) / 2, 0.012f), "Charcoal");
        Cylinder(g, "Shade", new Vector3(floorPos.x, shadeHeight, floorPos.z), new Vector3(0.36f, 0.07f, 0.36f), "Brass");
        Cylinder(g, "Glow", new Vector3(floorPos.x, shadeHeight - 0.072f, floorPos.z), new Vector3(0.3f, 0.004f, 0.3f), "LampGlow");
        if (withLight) PointLight(g, "Light", new Vector3(floorPos.x, shadeHeight - 0.2f, floorPos.z), new Color(1f, 0.78f, 0.55f), 0.7f, 3.2f);
    }

    static void HangingSign(Transform parent, string text, Vector3 pos, float yaw)
    {
        var g = Group("Sign_" + text, parent);
        g.position = pos;
        g.rotation = Quaternion.Euler(0, yaw, 0);
        BoxLocal(g, "Board", Vector3.zero, new Vector3(1.5f, 0.36f, 0.04f), "Walnut");
        BoxLocal(g, "Trim", new Vector3(0, -0.2f, 0), new Vector3(1.5f, 0.03f, 0.045f), "Brass");
        foreach (float x in new[] { -0.6f, 0.6f })
        {
            var cord = Cylinder(g, "Cord", Vector3.zero, new Vector3(0.008f, (3.0f - pos.y - 0.18f) / 2, 0.008f), "Charcoal");
            cord.localPosition = new Vector3(x, 0.18f + (3.0f - pos.y - 0.18f) / 2, 0);
        }
        foreach (float side in new[] { -1f, 1f })
        {
            var tgo = new GameObject("Text");
            tgo.transform.SetParent(g, false);
            tgo.transform.localPosition = new Vector3(0, 0.0f, side * 0.022f);
            tgo.transform.localRotation = Quaternion.Euler(0, side > 0 ? 180f : 0f, 0);
            var tmp = tgo.AddComponent<TextMeshPro>();
            tmp.text = text;
            tmp.fontSize = 2.2f;
            tmp.color = new Color(0.97f, 0.93f, 0.84f);
            tmp.alignment = TextAlignmentOptions.Center;
            tmp.rectTransform.sizeDelta = new Vector2(1.4f, 0.32f);
            tmp.enableWordWrapping = false;
        }
    }

    static void Chalkboard(Transform parent, Vector3 pos, string text)
    {
        var g = Group("Chalkboard", parent);
        g.position = pos;
        BoxLocal(g, "Frame", Vector3.zero, new Vector3(1.3f, 0.75f, 0.04f), "Oak");
        BoxLocal(g, "Board", new Vector3(0, 0, -0.012f), new Vector3(1.18f, 0.63f, 0.025f), "Chalk");
        var tgo = new GameObject("Text");
        tgo.transform.SetParent(g, false);
        tgo.transform.localPosition = new Vector3(0, 0, -0.03f);
        var tmp = tgo.AddComponent<TextMeshPro>();
        tmp.text = text;
        tmp.fontSize = 2.4f;
        tmp.color = new Color(0.95f, 0.94f, 0.88f);
        tmp.alignment = TextAlignmentOptions.Center;
        tmp.rectTransform.sizeDelta = new Vector2(1.15f, 0.6f);
        tmp.enableWordWrapping = false;
    }

    static void PottedPlant(Transform parent, Vector3 pos, float height)
    {
        var g = Group("PottedPlant", parent);
        float potR = 0.17f + height * 0.05f, potH = 0.3f + height * 0.08f;
        Cylinder(g, "Pot", pos + new Vector3(0, potH / 2, 0), new Vector3(potR * 2, potH / 2, potR * 2), "Terracotta");
        Cylinder(g, "Rim", pos + new Vector3(0, potH, 0), new Vector3(potR * 2.15f, 0.025f, potR * 2.15f), "Terracotta");
        Cylinder(g, "Soil", pos + new Vector3(0, potH + 0.01f, 0), new Vector3(potR * 1.9f, 0.01f, potR * 1.9f), "Soil");
        int clumps = 5 + (int)(height * 3);
        for (int i = 0; i < clumps; i++)
        {
            float a = (float)rng.NextDouble() * Mathf.PI * 2f;
            float r = (float)rng.NextDouble() * potR * 0.9f;
            float y = potH + 0.12f + (float)rng.NextDouble() * (height - potH - 0.15f);
            float s = 0.16f + (float)rng.NextDouble() * 0.16f * (1f + (1f - y / height));
            Sphere(g, "Leaves", pos + new Vector3(Mathf.Cos(a) * r, y, Mathf.Sin(a) * r), new Vector3(s * 1.2f, s, s * 1.2f), LeafMat());
        }
    }

    static void PottedTree(Transform parent, Vector3 pos, float height)
    {
        var g = Group("PottedTree", parent);
        Cylinder(g, "Pot", pos + new Vector3(0, 0.3f, 0), new Vector3(0.75f, 0.3f, 0.75f), "Terracotta");
        Cylinder(g, "Soil", pos + new Vector3(0, 0.61f, 0), new Vector3(0.68f, 0.01f, 0.68f), "Soil");
        Cylinder(g, "Trunk", pos + new Vector3(0, 0.6f + (height - 1.4f) / 2, 0), new Vector3(0.09f, (height - 1.4f) / 2 + 0.2f, 0.09f), "Walnut");
        for (int i = 0; i < 9; i++)
        {
            float a = i * 0.7f + (float)rng.NextDouble();
            float r = 0.15f + (float)rng.NextDouble() * 0.35f;
            float y = height - 0.75f + (float)rng.NextDouble() * 0.6f;
            float s = 0.45f + (float)rng.NextDouble() * 0.3f;
            Sphere(g, "Canopy", pos + new Vector3(Mathf.Cos(a) * r, y, Mathf.Sin(a) * r), new Vector3(s, s * 0.85f, s), LeafMat());
        }
    }

    static void PlanterBox(Transform parent, Vector3 pos, float length)
    {
        var g = Group("PlanterBox", parent);
        Box(g, "Box", pos + new Vector3(0, 0.22f, 0), new Vector3(length, 0.44f, 0.4f), "Oak");
        Box(g, "Soil", pos + new Vector3(0, 0.441f, 0), new Vector3(length - 0.06f, 0.004f, 0.34f), "Soil");
        int n = Mathf.RoundToInt(length / 0.22f);
        for (int i = 0; i < n; i++)
        {
            float x = -length / 2 + 0.12f + i * (length - 0.24f) / (n - 1);
            float s = 0.2f + (float)rng.NextDouble() * 0.12f;
            Sphere(g, "Shrub", pos + new Vector3(x, 0.5f + s * 0.3f, Jit(0.05f)), new Vector3(s, s * 0.9f, s), LeafMat());
        }
    }

    static void LivingWall(Transform parent, Vector3 center, float width, float height)
    {
        var g = Group("LivingWall", parent);
        Box(g, "Frame", center, new Vector3(width + 0.08f, height + 0.08f, 0.04f), "Walnut");
        int cols = Mathf.RoundToInt(width / 0.16f), rows = Mathf.RoundToInt(height / 0.16f);
        for (int i = 0; i < cols; i++)
        for (int j = 0; j < rows; j++)
        {
            float s = 0.14f + (float)rng.NextDouble() * 0.08f;
            var p = center + new Vector3(-width / 2 + (i + 0.5f) * width / cols + Jit(0.02f), -height / 2 + (j + 0.5f) * height / rows + Jit(0.02f), -0.04f);
            Sphere(g, "Leaf", p, new Vector3(s, s, s * 0.6f), LeafMat());
        }
    }

    static void CafeTable(Transform parent, Vector3 pos)
    {
        var g = Group("CafeTable", parent);
        Cylinder(g, "Top", pos + new Vector3(0, 0.74f, 0), new Vector3(0.78f, 0.02f, 0.78f), "OakLight");
        Cylinder(g, "Stem", pos + new Vector3(0, 0.37f, 0), new Vector3(0.06f, 0.36f, 0.06f), "Charcoal");
        Cylinder(g, "Foot", pos + new Vector3(0, 0.015f, 0), new Vector3(0.45f, 0.015f, 0.45f), "Charcoal");
        Prefab(g, FoodDir + "SaladBowl/SaladBowl.fbx", pos + new Vector3(0.12f, 0.76f, 0.05f), (float)rng.NextDouble() * 360f, 0.2f);
        Cylinder(g, "Cup", pos + new Vector3(-0.15f, 0.8f, -0.1f), new Vector3(0.07f, 0.045f, 0.07f), "Cream");
        foreach (float yaw in new[] { 90f, 270f })
        {
            var dir = Quaternion.Euler(0, yaw, 0) * Vector3.forward;
            Chair(g, pos + dir * 0.62f, yaw + 180f);
        }
    }

    static void Chair(Transform parent, Vector3 pos, float yaw)
    {
        var g = Group("Chair", parent);
        g.position = pos;
        g.rotation = Quaternion.Euler(0, yaw, 0);
        BoxLocal(g, "Seat", new Vector3(0, 0.45f, 0), new Vector3(0.42f, 0.04f, 0.42f), "Oak");
        BoxLocal(g, "Back", new Vector3(0, 0.72f, -0.19f), new Vector3(0.42f, 0.3f, 0.03f), "Oak");
        foreach (var s in new[] { new Vector2(-1, -1), new Vector2(1, -1), new Vector2(-1, 1), new Vector2(1, 1) })
            BoxLocal(g, "Leg", new Vector3(s.x * 0.18f, 0.215f, s.y * 0.18f), new Vector3(0.035f, 0.43f, 0.035f), "Walnut");
    }

    static void Bench(Transform parent, Vector3 pos, float yaw)
    {
        var g = Group("Bench", parent);
        g.position = pos;
        g.rotation = Quaternion.Euler(0, yaw, 0);
        for (int i = 0; i < 3; i++)
            BoxLocal(g, "Slat", new Vector3(0, 0.44f, -0.14f + i * 0.14f), new Vector3(1.5f, 0.035f, 0.11f), "Oak");
        foreach (float x in new[] { -0.62f, 0.62f })
            BoxLocal(g, "Leg", new Vector3(x, 0.21f, 0), new Vector3(0.06f, 0.42f, 0.4f), "Walnut");
    }

    static void Rug(Transform parent, Vector3 pos, Vector2 size, string mat)
    {
        Box(parent, "Rug", pos + new Vector3(0, 0.012f, 0), new Vector3(size.x, 0.008f, size.y), mat);
        Box(parent, "RugBorder", pos + new Vector3(0, 0.0105f, 0), new Vector3(size.x + 0.12f, 0.006f, size.y + 0.12f), "Linen");
    }

    static void FlowerCart(Transform parent, Vector3 pos, float yaw)
    {
        var g = Group("FlowerCart", parent);
        g.position = pos;
        g.rotation = Quaternion.Euler(0, yaw, 0);
        BoxLocal(g, "Bed", new Vector3(0, 0.62f, 0), new Vector3(1.3f, 0.36f, 0.62f), "OakLight");
        BoxLocal(g, "Rail", new Vector3(0, 0.82f, -0.3f), new Vector3(1.3f, 0.06f, 0.04f), "Walnut");
        foreach (float x in new[] { -0.55f, 0.55f })
        {
            var w = CylinderLocal(g, "Wheel", new Vector3(x, 0.23f, 0.33f), new Vector3(0.45f, 0.025f, 0.45f), "Walnut");
            w.localRotation = Quaternion.Euler(90, 0, 0);
            var w2 = CylinderLocal(g, "Wheel", new Vector3(x, 0.23f, -0.33f), new Vector3(0.45f, 0.025f, 0.45f), "Walnut");
            w2.localRotation = Quaternion.Euler(90, 0, 0);
        }
        BoxLocal(g, "Handle", new Vector3(0.8f, 0.85f, 0), new Vector3(0.35f, 0.04f, 0.04f), "Walnut");
        string[] blooms = { "FlowerPink", "FlowerYellow", "FlowerWhite", "FlowerLavender", "FlowerPink", "FlowerWhite" };
        for (int i = 0; i < 6; i++)
        {
            var bucketPos = new Vector3(-0.48f + (i % 3) * 0.48f, 0.8f, i < 3 ? -0.14f : 0.14f);
            CylinderLocal(g, "Bucket", bucketPos + new Vector3(0, 0.1f, 0), new Vector3(0.22f, 0.1f, 0.22f), "CartMetal");
            for (int f = 0; f < 7; f++)
            {
                var stemTop = bucketPos + new Vector3(Jit(0.08f), 0.38f + (float)rng.NextDouble() * 0.18f, Jit(0.08f));
                CylinderLocal(g, "Stem", (bucketPos + stemTop) / 2 + new Vector3(0, 0.1f, 0), new Vector3(0.012f, (stemTop.y - bucketPos.y) / 2, 0.012f), "Stem");
                SphereLocal(g, "Bloom", stemTop + new Vector3(0, 0.1f, 0), Vector3.one * (0.07f + (float)rng.NextDouble() * 0.03f), blooms[i]);
            }
        }
    }

    static void Cart(Transform parent, Vector3 pos, float yaw)
    {
        var g = Group("ShoppingCart", parent);
        g.position = pos;
        g.rotation = Quaternion.Euler(0, yaw, 0);
        const float bw = 0.5f, bl = 0.75f, bh = 0.4f, by = 0.48f, t = 0.012f;
        BoxLocal(g, "Floor", new Vector3(0, by, 0), new Vector3(bw, t, bl), "CartMetal");
        BoxLocal(g, "SideL", new Vector3(-bw / 2, by + bh / 2, 0), new Vector3(t, bh, bl), "CartMetal");
        BoxLocal(g, "SideR", new Vector3(bw / 2, by + bh / 2, 0), new Vector3(t, bh, bl), "CartMetal");
        BoxLocal(g, "Front", new Vector3(0, by + bh / 2, bl / 2), new Vector3(bw, bh, t), "CartMetal");
        BoxLocal(g, "Back", new Vector3(0, by + bh / 2, -bl / 2), new Vector3(bw, bh, t), "CartMetal");
        BoxLocal(g, "Handle", new Vector3(0, by + bh + 0.08f, -bl / 2 - 0.12f), new Vector3(bw + 0.04f, 0.03f, 0.03f), "Rubber");
        BoxLocal(g, "HandleArmL", new Vector3(-bw / 2, by + bh, -bl / 2 - 0.06f), new Vector3(0.015f, 0.12f, 0.14f), "CartMetal");
        BoxLocal(g, "HandleArmR", new Vector3(bw / 2, by + bh, -bl / 2 - 0.06f), new Vector3(0.015f, 0.12f, 0.14f), "CartMetal");
        BoxLocal(g, "Base", new Vector3(0, 0.12f, 0), new Vector3(bw - 0.06f, 0.02f, bl), "CartMetal");
        foreach (var s in new[] { new Vector2(-1, -1), new Vector2(1, -1), new Vector2(-1, 1), new Vector2(1, 1) })
        {
            BoxLocal(g, "Post", new Vector3(s.x * (bw / 2 - 0.03f), (by + 0.06f) / 2, s.y * (bl / 2 - 0.04f)), new Vector3(0.02f, by - 0.06f, 0.02f), "CartMetal");
            var w = CylinderLocal(g, "Wheel", new Vector3(s.x * (bw / 2 - 0.03f), 0.05f, s.y * (bl / 2 - 0.04f)), new Vector3(0.1f, 0.012f, 0.1f), "Rubber");
            w.localRotation = Quaternion.Euler(0, 0, 90);
        }
    }

    // ------------------------------------------------------------------ food helpers

    static float FoodSize(string item)
    {
        string k = item.ToLowerInvariant();
        if (k.Contains("strawberry")) return 0.04f;
        if (k.Contains("mushroom")) return 0.06f;
        if (k.Contains("lemon") || k.Contains("tomato") || k.Contains("onion")) return 0.075f;
        if (k.Contains("apple") || k.Contains("pear")) return 0.085f;
        if (k.Contains("joghurt") || k.Contains("mozzarella")) return 0.09f;
        if (k.Contains("cheese")) return 0.11f;
        if (k.Contains("can")) return 0.1f;
        if (k.Contains("carrot") || k.Contains("cucumber")) return 0.19f;
        if (k.Contains("milk")) return 0.2f;
        if (k.Contains("soap") || k.Contains("dough")) return 0.18f;
        if (k.Contains("spray") || k.Contains("chips")) return 0.24f;
        if (k.Contains("beerbox")) return 0.26f;
        if (k.Contains("bottle")) return 0.28f;
        return 0.12f;
    }

    /// Instantiates a food asset scaled so its largest dimension equals targetSize.
    static GameObject Food(Transform parent, string path, float targetSize, Vector3 pos, float yaw)
    {
        var asset = AssetDatabase.LoadAssetAtPath<GameObject>(path);
        if (asset == null) { Warn("Missing asset " + path); return null; }
        var go = (GameObject)PrefabUtility.InstantiatePrefab(asset, parent);
        go.transform.position = pos;
        go.transform.rotation = Quaternion.Euler(0, yaw, 0) * asset.transform.rotation;
        var b = Bounds(go);
        float max = Mathf.Max(b.size.x, b.size.y, b.size.z);
        if (max > 1e-5f) go.transform.localScale = asset.transform.localScale * (targetSize / max);
        foreach (var c in go.GetComponentsInChildren<Collider>()) Object.DestroyImmediate(c);
        return go;
    }

    static GameObject Prefab(Transform parent, string path, Vector3 pos, float yaw, float targetSize)
    {
        var go = Food(parent, path, targetSize, pos, yaw);
        if (go != null) SitOn(go, pos.y);
        return go;
    }

    static void SitOn(GameObject go, float surfaceY)
    {
        var b = Bounds(go);
        go.transform.position += new Vector3(0, surfaceY - b.min.y, 0);
    }

    static float Top(GameObject go) => Bounds(go).max.y;

    static void Row(Transform parent, string path, Vector3 center, float size, int count, float spacing, float yaw, Vector3 axis)
    {
        for (int i = 0; i < count; i++)
        {
            var p = center + axis * ((i - (count - 1) / 2f) * spacing);
            var go = Food(parent, path, size, p, yaw + Jit(8f));
            if (go != null) SitOn(go, center.y);
        }
    }

    static void Grid(Transform parent, string path, Vector3 center, float size, int nx, int nz, float sx, float sz)
    {
        for (int i = 0; i < nx; i++)
        for (int j = 0; j < nz; j++)
        {
            var p = center + new Vector3((i - (nx - 1) / 2f) * sx, 0, (j - (nz - 1) / 2f) * sz);
            var go = Food(parent, path, size, p, Jit(10f));
            if (go != null) SitOn(go, center.y);
        }
    }

    static void FillCircle(Transform parent, string path, float y, Vector3 center, float radius, int count, float size, bool upright)
    {
        for (int i = 0; i < count; i++)
        {
            float a = i * Mathf.PI * 2f / count;
            var p = new Vector3(center.x + Mathf.Cos(a) * radius * 0.5f, y, center.z + Mathf.Sin(a) * radius * 0.5f);
            var go = Food(parent, path, size, p, a * Mathf.Rad2Deg);
            if (go == null) continue;
            if (upright)
            {
                // Stand the longest axis vertically, leaning slightly outward.
                var b = Bounds(go);
                if (b.size.y < Mathf.Max(b.size.x, b.size.z)) go.transform.rotation = Quaternion.Euler(b.size.x > b.size.z ? 0 : 90, 0, b.size.x > b.size.z ? 90 : 0) * go.transform.rotation;
                go.transform.rotation = Quaternion.Euler(Mathf.Sin(a) * 10f, 0, -Mathf.Cos(a) * 10f) * go.transform.rotation;
            }
            SitOn(go, y);
        }
    }

    // ------------------------------------------------------------------ primitives

    static Transform Group(string name, Transform parent = null)
    {
        var go = new GameObject(name);
        go.transform.SetParent(parent != null ? parent : root, false);
        return go.transform;
    }

    static Transform Box(Transform parent, string name, Vector3 worldPos, Vector3 size, string mat)
    {
        var t = Prim(PrimitiveType.Cube, parent, name, mat);
        t.position = worldPos;
        t.localScale = size;
        return t;
    }

    static Transform BoxLocal(Transform parent, string name, Vector3 localPos, Vector3 size, string mat)
    {
        var t = Prim(PrimitiveType.Cube, parent, name, mat);
        t.localPosition = localPos;
        t.localRotation = Quaternion.identity;
        t.localScale = size;
        return t;
    }

    /// Unity's cylinder is 2 units tall, so scale.y is the half height.
    static Transform Cylinder(Transform parent, string name, Vector3 worldPos, Vector3 scale, string mat)
    {
        var t = Prim(PrimitiveType.Cylinder, parent, name, mat);
        t.position = worldPos;
        t.localScale = scale;
        return t;
    }

    static Transform CylinderLocal(Transform parent, string name, Vector3 localPos, Vector3 scale, string mat)
    {
        var t = Prim(PrimitiveType.Cylinder, parent, name, mat);
        t.localPosition = localPos;
        t.localScale = scale;
        return t;
    }

    static Transform Sphere(Transform parent, string name, Vector3 worldPos, Vector3 scale, string mat)
    {
        var t = Prim(PrimitiveType.Sphere, parent, name, mat);
        t.position = worldPos;
        t.localScale = scale;
        return t;
    }

    static Transform SphereLocal(Transform parent, string name, Vector3 localPos, Vector3 scale, string mat)
    {
        var t = Prim(PrimitiveType.Sphere, parent, name, mat);
        t.localPosition = localPos;
        t.localScale = scale;
        return t;
    }

    static Transform Prim(PrimitiveType type, Transform parent, string name, string mat)
    {
        var go = GameObject.CreatePrimitive(type);
        go.name = name;
        Object.DestroyImmediate(go.GetComponent<Collider>());
        go.transform.SetParent(parent, false);
        var r = go.GetComponent<MeshRenderer>();
        if (mats.TryGetValue(mat, out var m)) r.sharedMaterial = m; else Warn("Missing material " + mat);
        r.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
        return go.transform;
    }

    static string LeafMat()
    {
        int i = rng.Next(3);
        return i == 0 ? "LeafDark" : i == 1 ? "Leaf" : "LeafLight";
    }

    static float Jit(float amount) => ((float)rng.NextDouble() * 2f - 1f) * amount;

    static Bounds Bounds(GameObject go)
    {
        var rs = go.GetComponentsInChildren<Renderer>();
        if (rs.Length == 0) return new Bounds(go.transform.position, Vector3.zero);
        var b = rs[0].bounds;
        for (int i = 1; i < rs.Length; i++) b.Encapsulate(rs[i].bounds);
        return b;
    }

    static void Warn(string w) { if (!warnings.Contains(w)) warnings.Add(w); }

    // ------------------------------------------------------------------ colliders + checks

    /// One box collider per floor-standing prop so the CharacterController walks around it.
    /// Overhead items (signs, lamps), rugs, floor decks and wall-mounted panels get none.
    static void FinalizeColliders()
    {
        var skip = new HashSet<string> { "WoodFloor", "ZoneLights", "PendantLamp", "LivingWall", "Chalkboard" };
        foreach (var t in root.GetComponentsInChildren<Transform>())
        {
            var go = t.gameObject;
            GameObjectUtility.SetStaticEditorFlags(go, StaticEditorFlags.BatchingStatic | StaticEditorFlags.OccludeeStatic);
        }

        foreach (var prop in PropRoots())
        {
            if (skip.Contains(prop.name) || prop.name.StartsWith("Sign_") || prop.name.StartsWith("Rug") || prop.name == "AccentWall") continue;
            var b = Bounds(prop.gameObject);
            if (b.size.y < 0.05f) continue;
            var col = prop.gameObject.AddComponent<BoxCollider>();
            // Bounds are world space; convert to the prop's local space (props are unscaled).
            var inv = Quaternion.Inverse(prop.rotation);
            var localCenter = inv * (b.center - prop.position);
            var localSize = inv * b.size;
            col.center = localCenter;
            col.size = new Vector3(Mathf.Abs(localSize.x), Mathf.Abs(localSize.y), Mathf.Abs(localSize.z));
        }
    }

    /// Direct children of zone groups, i.e. the individual props.
    static IEnumerable<Transform> PropRoots()
    {
        foreach (Transform zone in root)
        foreach (Transform prop in zone)
            yield return prop;
    }

    static void CheckPlacement()
    {
        Physics.SyncTransforms();
        var level = GameObject.Find("_Level");
        foreach (var prop in PropRoots())
        {
            var col = prop.GetComponent<BoxCollider>();
            if (col == null) continue;
            var b = col.bounds;

            // Overlap with level walls (floor colliders excluded by lifting the test box 5 cm).
            var hits = Physics.OverlapBox(b.center + Vector3.up * 0.05f, b.extents - new Vector3(0.02f, 0.05f, 0.02f), Quaternion.identity);
            foreach (var h in hits)
                if (level != null && h.transform.IsChildOf(level.transform) && !h.name.StartsWith("Floor"))
                    Warn(prop.parent.name + "/" + prop.name + " at " + Fmt(b.center) + " overlaps level collider " + h.name);

            // Clearance from the auto-move route.
            float d = RouteDistance(b);
            if (d < RouteClearance) Warn(prop.parent.name + "/" + prop.name + " at " + Fmt(b.center) + " is " + d.ToString("0.00") + " m from the auto-move route");

            // Clearance from the spawn.
            if (b.SqrDistance(new Vector3(0, b.center.y, 0)) < 1.0f) Warn(prop.name + " is within 1 m of the spawn");
        }
    }

    static float RouteDistance(Bounds b)
    {
        float best = float.MaxValue;
        for (int i = 0; i < Route.Length - 1; i++)
            for (float s = 0; s <= 1f; s += 0.02f)
            {
                var p = Vector2.Lerp(Route[i], Route[i + 1], s);
                float dx = Mathf.Max(b.min.x - p.x, 0, p.x - b.max.x);
                float dz = Mathf.Max(b.min.z - p.y, 0, p.y - b.max.z);
                best = Mathf.Min(best, Mathf.Sqrt(dx * dx + dz * dz));
            }
        return best;
    }

    static string Fmt(Vector3 v) => "(" + v.x.ToString("0.0") + ", " + v.z.ToString("0.0") + ")";

}
