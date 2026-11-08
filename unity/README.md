# Unity Environments

This folder contains the Unity environments used by the BCI project.

## Environments

- `environment_1/`: the indoor market, Unity 2022.3.0f1. Builds for Quest with the Oculus XR plugin.
- `environment_1_unity6/`: the same indoor market, Unity 6000.6.8f1. Builds for Quest with OpenXR and the Meta Quest feature, because Unity 6.6 no longer supports the Oculus XR plugin.

The two environment one folders are separate copies, so a change made in one does not
reach the other. Scene layout comes from `Assets/Editor/RelaxedMarketBuilder.cs`: edit
that file in both folders and run **BCI > Rebuild Relaxed Market** in each. Open each
folder only with its own Unity version, because opening a project in another version
upgrades its files.

## Required Unity folders

Each environment includes only the required Unity project folders:

- `Assets/`
- `Packages/`
- `ProjectSettings/`

Generated folders such as `Library/`, `Temp/`, `Obj/`, `Build/`, `Logs/`, and `UserSettings/` are excluded.

## Opening the projects

1. Open Unity Hub.
2. Select **Add project from disk**.
3. Choose `unity/environment_1/` or `unity/environment_1_unity6/`.
4. Open the project using the Unity version specified in `ProjectSettings/ProjectVersion.txt`.
