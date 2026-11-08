using System;
using UnityEngine;

[DisallowMultipleComponent]
public class FreeNavController : MonoBehaviour
{
    [Header("Mode")]
    [Tooltip("If a legacy BCICommandClient exists, this controller stands down.")]
    [SerializeField] private bool disableWhenLegacyBCIClientPresent = true;

    [Header("Input")]
    [SerializeField] private PredictionWebSocketClient predictionClient;

    [Header("Rig")]
    [SerializeField] private Transform rigRoot;
    [SerializeField] private CharacterController characterController;
    [SerializeField] private Camera hmdCamera;

    [Header("Movement")]
    [SerializeField] private float moveSpeedMetersPerSecond = 1.0f;
    [SerializeField] private float rotateDegreesPerSecond = 90.0f;
    [SerializeField] private float predictionTimeoutSeconds = 0.5f;
    [SerializeField] private float maxFrameAgeSeconds = 2f;
    [SerializeField] private bool logMovement;

    [Header("Threshold gating")]
    [SerializeField, Range(0f, 1f)] private float forwardThreshold = 0.55f;
    [SerializeField, Range(0f, 1f)] private float backwardThreshold = 0.55f;
    [SerializeField, Range(0f, 1f)] private float rotateLeftThreshold = 0.55f;
    [SerializeField, Range(0f, 1f)] private float rotateRightThreshold = 0.55f;
    [SerializeField, Range(0f, 1f)] private float blinkThreshold = 0.60f;

    private PredictionWebSocketClient.PredictionMessage latestPrediction;
    private float lastPredictionTime;
    private string activeMovement;

    private void Awake()
    {
        if (rigRoot == null)
        {
            rigRoot = transform;
        }

        if (hmdCamera == null)
        {
            hmdCamera = Camera.main;
        }

        if (characterController == null && rigRoot != null)
        {
            characterController = rigRoot.GetComponent<CharacterController>();
        }
    }

    private void OnEnable()
    {
        if (predictionClient != null)
        {
            predictionClient.OnPrediction += HandlePrediction;
            predictionClient.OnDisconnected += ClearPrediction;
        }
    }

    private void OnDisable()
    {
        if (predictionClient != null)
        {
            predictionClient.OnPrediction -= HandlePrediction;
            predictionClient.OnDisconnected -= ClearPrediction;
        }
        ClearPrediction();
    }

    private void OnApplicationPause(bool paused)
    {
        if (paused)
        {
            ClearPrediction();
        }
    }

    private void Update()
    {
        if (disableWhenLegacyBCIClientPresent && FindObjectOfType<BCI.BCICommandClient>() != null)
        {
            return;
        }

        if (latestPrediction == null || rigRoot == null ||
            predictionClient == null || !predictionClient.IsConnected ||
            Time.unscaledTime - lastPredictionTime > predictionTimeoutSeconds)
        {
            LogMovement(null);
            return;
        }

        if (!latestPrediction.stable)
        {
            LogMovement(null);
            return;
        }

        string label = latestPrediction.predicted_class ?? string.Empty;
        float confidence = latestPrediction.confidence;
        float dt = Time.deltaTime;
        bool moved = true;

        if (label == "mi_forward" && confidence >= forwardThreshold)
        {
            Move(GetFlatForward() * moveSpeedMetersPerSecond * dt);
        }
        else if (label == "mi_backward" && confidence >= backwardThreshold)
        {
            Move(-GetFlatForward() * moveSpeedMetersPerSecond * dt);
        }
        else if ((label == "mi_rotate_left" || label == "left_motor_imagery") &&
                 confidence >= rotateLeftThreshold)
        {
            rigRoot.Rotate(Vector3.up, -rotateDegreesPerSecond * dt, Space.World);
        }
        else if ((label == "mi_rotate_right" || label == "right_motor_imagery") &&
                 confidence >= rotateRightThreshold)
        {
            rigRoot.Rotate(Vector3.up, rotateDegreesPerSecond * dt, Space.World);
        }
        else if (label == "intentional_blink" && confidence >= blinkThreshold)
        {
            // Reserved action: blink can be mapped to interact/jump/confirm later.
            moved = false;
        }
        else
        {
            moved = false;
        }
        LogMovement(moved ? label : null);
    }

    private void HandlePrediction(PredictionWebSocketClient.PredictionMessage prediction)
    {
        if (prediction == null || prediction.type != "prediction")
        {
            return;
        }
        double ageSeconds = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - prediction.timestamp;
        if (prediction.timestamp <= 0 || ageSeconds < -maxFrameAgeSeconds ||
            ageSeconds > maxFrameAgeSeconds)
        {
            return;
        }
        latestPrediction = prediction;
        lastPredictionTime = Time.unscaledTime;
    }

    private void ClearPrediction()
    {
        latestPrediction = null;
    }

    private void LogMovement(string label)
    {
        if (!logMovement || activeMovement == label)
        {
            return;
        }
        activeMovement = label;
        Debug.Log("[FreeNavController] " + (label ?? "stopped") +
                  " rig=" + rigRoot.position + " yaw=" + rigRoot.eulerAngles.y);
    }

    private void Move(Vector3 delta)
    {
        if (characterController != null)
        {
            characterController.Move(delta);
        }
        else
        {
            rigRoot.position += delta;
        }
    }

    private Vector3 GetFlatForward()
    {
        Vector3 forward = hmdCamera != null ? hmdCamera.transform.forward : rigRoot.forward;
        forward.y = 0f;
        if (forward.sqrMagnitude < 0.0001f)
        {
            forward = rigRoot.forward;
            forward.y = 0f;
        }
        return forward.sqrMagnitude < 0.0001f ? Vector3.forward : forward.normalized;
    }
}
