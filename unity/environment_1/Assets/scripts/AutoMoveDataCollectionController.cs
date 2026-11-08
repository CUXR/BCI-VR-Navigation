using System;
using System.Collections;
using System.Globalization;
using System.IO;
using TMPro;
using Unity.XR.CoreUtils;
using UnityEngine;

public class AutoMoveDataCollectionController : MonoBehaviour
{
    private enum ActionType
    {
        Movement,
        Rotation
    }

    private enum ActionDirection
    {
        Forward,
        Backward,
        Rightward,
        Leftward
    }

    private struct SequenceAction
    {
        public readonly ActionType ActionType;
        public readonly ActionDirection Direction;
        public readonly string CueText;
        public readonly string MovementText;

        public SequenceAction(ActionType actionType, ActionDirection direction, string cueText, string movementText)
        {
            ActionType = actionType;
            Direction = direction;
            CueText = cueText;
            MovementText = movementText;
        }
    }

    [Header("XR Rig")]
    [Tooltip("XR Origin or player rig root to move. If empty, the script finds the scene XR Origin or the Main Camera parent rig.")]
    [SerializeField] private Transform xrRigRoot;

    [Tooltip("HMD camera used for movement direction and instruction placement. If empty, Camera.main is used.")]
    [SerializeField] private Camera hmdCamera;

    [Tooltip("Optional. If assigned or found on the rig root, movement uses CharacterController.Move.")]
    [SerializeField] private CharacterController characterController;

    [Header("Timing")]
    [SerializeField, Min(0f)] private float cueDuration = 2f;
    [SerializeField, Min(0.01f)] private float movementDuration = 3f;

    [Header("Movement")]
    [SerializeField, Min(0f)] private float movementSpeed = 1f;
    [SerializeField, Min(0f)] private float rotationAngle = 90f;

    [Header("Instruction Text")]
    [SerializeField, Min(0.1f)] private float instructionTextDistanceFromCamera = 2f;
    [SerializeField, Min(0.01f)] private float instructionTextSize = 0.25f;
    [SerializeField] private TMP_Text instructionText;

    [Header("Runtime")]
    [SerializeField] private bool autoStartOnSceneStart = true;
    [SerializeField, Min(1)] private int sequenceRuns = 1;

    [Header("Networking")]
    [Tooltip("Optional marker socket client; when present, emits marker/control JSON to Python collector.")]
    [SerializeField] private MarkerWebSocketClient markerWebSocketClient;

    private static readonly SequenceAction[] Sequence =
    {
        // Outbound path.
        new SequenceAction(ActionType.Rotation, ActionDirection.Rightward, "Now imagine rotating rightward", "Rotating rightward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Rightward, "Now imagine rotating rightward", "Rotating rightward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Rightward, "Now imagine rotating rightward", "Rotating rightward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Rightward, "Now imagine rotating rightward", "Rotating rightward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Leftward, "Now imagine rotating leftward", "Rotating leftward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Forward, "Now imagine moving forward", "Moving forward"),

        // Return path: reverse the outbound path and invert each action.
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Rightward, "Now imagine rotating rightward", "Rotating rightward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Leftward, "Now imagine rotating leftward", "Rotating leftward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Leftward, "Now imagine rotating leftward", "Rotating leftward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Movement, ActionDirection.Backward, "Now imagine moving backward", "Moving backward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Leftward, "Now imagine rotating leftward", "Rotating leftward"),
        new SequenceAction(ActionType.Rotation, ActionDirection.Leftward, "Now imagine rotating leftward", "Rotating leftward")
    };

    private Coroutine activeSequence;
    private StreamWriter logWriter;
    private string logFilePath;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    private static void BootstrapOnSceneLoad()
    {
        if (FindObjectOfType<AutoMoveDataCollectionController>() != null)
        {
            return;
        }

        GameObject controllerObject = new GameObject("Auto Move Data Collection Controller");
        controllerObject.AddComponent<AutoMoveDataCollectionController>();
    }

    private void Start()
    {
        ResolveSceneReferences();
        EnsureInstructionText();

        if (autoStartOnSceneStart)
        {
            StartSequence();
        }
    }

    private void Update()
    {
        UpdateInstructionTextTransform();
    }

    private void OnDestroy()
    {
        CloseLog();
    }

    private void OnApplicationQuit()
    {
        CloseLog();
    }

    [ContextMenu("Start Data Collection Sequence")]
    public void StartSequence()
    {
        ResolveSceneReferences();
        EnsureInstructionText();

        if (xrRigRoot == null)
        {
            Debug.LogError("AutoMoveDataCollectionController could not find an XR rig root to move.");
            return;
        }

        if (activeSequence != null)
        {
            StopCoroutine(activeSequence);
        }

        activeSequence = StartCoroutine(RunSequence());
    }

    [ContextMenu("Stop Data Collection Sequence")]
    public void StopSequence()
    {
        if (activeSequence != null)
        {
            StopCoroutine(activeSequence);
            activeSequence = null;
        }

        SetInstructionText(string.Empty);
        CloseLog();
    }

    private IEnumerator RunSequence()
    {
        OpenLog();
        int runCount = Mathf.Max(1, sequenceRuns);
        for (int runIndex = 0; runIndex < runCount; runIndex++)
        {
            markerWebSocketClient?.EmitControl("run_start", runIndex);
            for (int i = 0; i < Sequence.Length; i++)
            {
                int stepIndex = i + 1;
                SequenceAction action = Sequence[i];

                SetInstructionText(action.CueText);
                LogPhase(stepIndex, runIndex, action, "cue", action.CueText);
                yield return new WaitForSeconds(cueDuration);

                SetInstructionText(action.MovementText);
                LogPhase(stepIndex, runIndex, action, "start", action.MovementText);

                if (action.ActionType == ActionType.Movement)
                {
                    yield return MoveForDuration(action);
                }
                else
                {
                    yield return RotateForDuration(action);
                }

                LogPhase(stepIndex, runIndex, action, "end", action.MovementText);
            }
            markerWebSocketClient?.EmitControl("run_end", runIndex);
        }

        SetInstructionText("Data collection sequence complete");
        markerWebSocketClient?.EmitControl("session_end", Mathf.Max(0, runCount - 1));
        activeSequence = null;
        CloseLog();
    }

    private IEnumerator MoveForDuration(SequenceAction action)
    {
        float elapsed = 0f;
        float directionSign = action.Direction == ActionDirection.Backward ? -1f : 1f;

        while (elapsed < movementDuration)
        {
            float deltaTime = Mathf.Min(Time.deltaTime, movementDuration - elapsed);
            Vector3 flatForward = GetFlatCameraForward();
            Vector3 motion = flatForward * directionSign * movementSpeed * deltaTime;
            MoveRig(motion);

            elapsed += deltaTime;
            yield return null;
        }
    }

    private IEnumerator RotateForDuration(SequenceAction action)
    {
        float elapsed = 0f;
        float directionSign = action.Direction == ActionDirection.Leftward ? -1f : 1f;
        Quaternion startRotation = xrRigRoot.rotation;
        Quaternion targetRotation = Quaternion.AngleAxis(rotationAngle * directionSign, Vector3.up) * startRotation;

        while (elapsed < movementDuration)
        {
            elapsed += Time.deltaTime;
            float t = Mathf.Clamp01(elapsed / movementDuration);
            xrRigRoot.rotation = Quaternion.Slerp(startRotation, targetRotation, t);
            yield return null;
        }

        xrRigRoot.rotation = targetRotation;
    }

    private void ResolveSceneReferences()
    {
        XROrigin xrOrigin = FindObjectOfType<XROrigin>();
        if (xrOrigin != null)
        {
            if (xrRigRoot == null)
            {
                xrRigRoot = xrOrigin.transform;
            }

            if (hmdCamera == null && xrOrigin.Camera != null)
            {
                hmdCamera = xrOrigin.Camera;
            }
        }

        if (hmdCamera == null)
        {
            hmdCamera = Camera.main != null ? Camera.main : FindObjectOfType<Camera>();
        }

        if (xrRigRoot == null && hmdCamera != null)
        {
            xrRigRoot = FindRigRootFromCamera(hmdCamera.transform);
        }

        if (characterController == null && xrRigRoot != null)
        {
            characterController = xrRigRoot.GetComponent<CharacterController>();
        }
    }

    private Transform FindRigRootFromCamera(Transform cameraTransform)
    {
        Transform current = cameraTransform;
        while (current != null)
        {
            if (current.name.IndexOf("XR Origin", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return current;
            }

            current = current.parent;
        }

        Transform parent = cameraTransform.parent;
        while (parent != null && parent.parent != null)
        {
            parent = parent.parent;
        }

        return parent != null ? parent : cameraTransform;
    }

    private void EnsureInstructionText()
    {
        if (instructionText != null)
        {
            instructionText.fontSize = instructionTextSize;
            return;
        }

        // If TextMeshPro is not configured in a different Unity project, create a
        // world-space TextMeshPro object manually and assign it to Instruction Text.
        GameObject textObject = new GameObject("Data Collection Instruction Text");
        TextMeshPro generatedText = textObject.AddComponent<TextMeshPro>();
        generatedText.alignment = TextAlignmentOptions.Center;
        generatedText.color = Color.white;
        generatedText.enableWordWrapping = false;
        generatedText.fontSize = instructionTextSize;
        generatedText.rectTransform.sizeDelta = new Vector2(5f, 1f);
        generatedText.text = string.Empty;
        instructionText = generatedText;
    }

    private void UpdateInstructionTextTransform()
    {
        if (instructionText == null || hmdCamera == null)
        {
            return;
        }

        Transform textTransform = instructionText.transform;
        Transform cameraTransform = hmdCamera.transform;
        textTransform.position = cameraTransform.position + cameraTransform.forward * instructionTextDistanceFromCamera;
        textTransform.rotation = Quaternion.LookRotation(textTransform.position - cameraTransform.position, cameraTransform.up);
    }

    private Vector3 GetFlatCameraForward()
    {
        Vector3 forward = hmdCamera != null ? hmdCamera.transform.forward : xrRigRoot.forward;
        forward.y = 0f;

        if (forward.sqrMagnitude < 0.0001f)
        {
            forward = xrRigRoot.forward;
            forward.y = 0f;
        }

        if (forward.sqrMagnitude < 0.0001f)
        {
            return Vector3.forward;
        }

        return forward.normalized;
    }

    private void MoveRig(Vector3 motion)
    {
        if (characterController != null)
        {
            characterController.Move(motion);
            return;
        }

        xrRigRoot.position += motion;
    }

    private void SetInstructionText(string value)
    {
        if (instructionText != null)
        {
            instructionText.text = value;
        }
    }

    private void OpenLog()
    {
        CloseLog();

        logFilePath = Path.Combine(Application.persistentDataPath, "auto_movement_session_log.csv");
        logWriter = new StreamWriter(logFilePath, false);
        logWriter.WriteLine("step_index,action_type,direction,phase,unity_time,real_timestamp,instruction_text,position,rotation");
        logWriter.Flush();

        Debug.Log("Auto movement session log: " + logFilePath);
    }

    private void LogPhase(int stepIndex, int runIndex, SequenceAction action, string phase, string instruction)
    {
        SendEEGMarker(stepIndex - 1, runIndex, action, phase);

        if (logWriter == null)
        {
            return;
        }

        Vector3 position = xrRigRoot != null ? xrRigRoot.position : Vector3.zero;
        Vector3 rotation = xrRigRoot != null ? xrRigRoot.eulerAngles : Vector3.zero;

        logWriter.WriteLine(string.Join(",",
            stepIndex.ToString(CultureInfo.InvariantCulture),
            EscapeCsv(ToLogValue(action.ActionType)),
            EscapeCsv(ToLogValue(action.Direction)),
            EscapeCsv(phase),
            Time.time.ToString("F6", CultureInfo.InvariantCulture),
            EscapeCsv(DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture)),
            EscapeCsv(instruction),
            EscapeCsv(FormatVector(position)),
            EscapeCsv(FormatVector(rotation))));
        logWriter.Flush();
    }

    private void CloseLog()
    {
        if (logWriter == null)
        {
            return;
        }

        logWriter.Flush();
        logWriter.Close();
        logWriter = null;
    }

    private string FormatVector(Vector3 value)
    {
        return string.Format(
            CultureInfo.InvariantCulture,
            "{0:F4};{1:F4};{2:F4}",
            value.x,
            value.y,
            value.z);
    }

    private string ToLogValue(ActionType value)
    {
        return value == ActionType.Movement ? "movement" : "rotation";
    }

    private string ToLogValue(ActionDirection value)
    {
        switch (value)
        {
            case ActionDirection.Forward:
                return "forward";
            case ActionDirection.Backward:
                return "backward";
            case ActionDirection.Rightward:
                return "right_rotate";
            case ActionDirection.Leftward:
                return "left_rotate";
            default:
                return value.ToString().ToLowerInvariant();
        }
    }

    private string EscapeCsv(string value)
    {
        if (value == null)
        {
            return string.Empty;
        }

        return "\"" + value.Replace("\"", "\"\"") + "\"";
    }

    private string ToProtocolDirection(ActionDirection value)
    {
        switch (value)
        {
            case ActionDirection.Forward:
                return "forward";
            case ActionDirection.Backward:
                return "backward";
            case ActionDirection.Rightward:
                return "right_rotate";
            case ActionDirection.Leftward:
                return "left_rotate";
            default:
                return "forward";
        }
    }

    private void SendEEGMarker(int trialIndex, int runIndex, SequenceAction action, string phase)
    {
        string direction = ToProtocolDirection(action.Direction);
        markerWebSocketClient?.EmitMarker(
            phase: phase,
            direction: direction,
            trialIndex: trialIndex,
            runIndex: runIndex
        );
        Debug.Log($"[EEG Marker] run={runIndex} trial={trialIndex} phase={phase} direction={direction}");
    }
}
