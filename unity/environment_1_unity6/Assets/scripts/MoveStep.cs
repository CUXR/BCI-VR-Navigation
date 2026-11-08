using UnityEngine;

public enum MoveDirection
{
    Forward,
    Back,
    Left,
    Right
}

[System.Serializable]
public struct MoveStep
{
    public MoveDirection direction;

    [Min(0f)]
    public float distanceMeters;

    [Min(0.01f)]
    public float durationSeconds;
}
