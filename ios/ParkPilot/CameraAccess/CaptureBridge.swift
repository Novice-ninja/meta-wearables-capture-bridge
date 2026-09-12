import AVFoundation
import Foundation
import Observation
import UIKit

struct CaptureManifestResponse: Decodable {
  let captureId: String

  enum CodingKeys: String, CodingKey {
    case captureId = "capture_id"
  }
}

/// Collects a short voice instruction from the current iOS audio route and
/// uploads it with the next DAT photo. When Ray-Ban audio is available as a
/// Bluetooth HFP input, AVAudioSession selects that route; otherwise iPhone mic
/// is used. The server manifest records the actual input route.
@Observable
@MainActor
final class CaptureBridge {
  var instruction = ""
  private(set) var isRecordingInstruction = false
  private(set) var isUploading = false
  private(set) var status = "Ready to expose the next capture"
  private(set) var lastCaptureId: String?

  private var recorder: AVAudioRecorder?
  private var audioURL: URL?
  private var recordedAudioInputName = "none"
  private var recordedAudioInputType = "none"

  func toggleInstructionRecording() {
    if isRecordingInstruction {
      stopInstructionRecording()
      return
    }
    AVAudioApplication.requestRecordPermission { [weak self] granted in
      Task { @MainActor in
        guard let self else { return }
        if granted {
          self.startInstructionRecording()
        } else {
          self.status = "Microphone permission denied"
        }
      }
    }
  }

  private func startInstructionRecording() {
    do {
      let session = AVAudioSession.sharedInstance()
      try session.setCategory(.record, mode: .spokenAudio, options: [.allowBluetoothHFP])
      try session.setActive(true)
      if let glassesInput = session.availableInputs?.first(where: { $0.portType == .bluetoothHFP }) {
        try session.setPreferredInput(glassesInput)
      }
      recordedAudioInputName = session.currentRoute.inputs.first?.portName ?? "unknown"
      recordedAudioInputType = session.currentRoute.inputs.first?.portType.rawValue ?? "unknown"
      cleanupAudio()
      let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("wearable-instruction-\(UUID().uuidString).m4a")
      recorder = try AVAudioRecorder(
        url: url,
        settings: [
          AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
          AVSampleRateKey: 16_000,
          AVNumberOfChannelsKey: 1,
          AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]
      )
      guard recorder?.record() == true else {
        throw NSError(
          domain: "CaptureBridge",
          code: 2,
          userInfo: [NSLocalizedDescriptionKey: "The active audio input could not start recording."]
        )
      }
      audioURL = url
      isRecordingInstruction = true
      status = "Recording instruction from active audio input"
    } catch {
      status = "Audio error: \(error.localizedDescription)"
    }
  }

  func stopInstructionRecording() {
    recorder?.stop()
    recorder = nil
    isRecordingInstruction = false
    try? AVAudioSession.sharedInstance().setActive(false)
    status = "Voice instruction attached"
  }

  func upload(photoData: Data) async {
    if isRecordingInstruction { stopInstructionRecording() }
    guard let baseURLText = Bundle.main.object(forInfoDictionaryKey: "CaptureBridgeURL") as? String,
      let url = URL(string: baseURLText)?.appendingPathComponent("v1/captures")
    else {
      status = "Set CaptureBridgeURL in Info.plist"
      return
    }

    isUploading = true
    status = "Uploading capture…"
    defer { isUploading = false }

    do {
      let source: [String: Any] = [
        "platform": "ios",
        "capture_device": "meta_wearable",
        "sdk": "meta-wearables-dat-ios",
        "sdk_version": "0.9.0",
      ]
      var metadata: [String: Any] = [
        "image_origin": "MWDATCamera.PhotoData",
        "audio_input": recordedAudioInputName,
        "audio_input_type": recordedAudioInputType,
      ]
      metadata["phone_model"] = UIDevice.current.model

      let boundary = "CaptureBridge-\(UUID().uuidString)"
      var body = Data()
      body.appendFormField("captured_at", value: ISO8601DateFormatter().string(from: Date()), boundary: boundary)
      body.appendFormField("source_json", value: try jsonString(source), boundary: boundary)
      body.appendFormField("metadata_json", value: try jsonString(metadata), boundary: boundary)
      if !instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
        body.appendFormField("instruction", value: instruction, boundary: boundary)
      }
      body.appendFileField("image", filename: "capture.jpg", contentType: "image/jpeg", data: photoData, boundary: boundary)
      if let audioURL, let audioData = try? Data(contentsOf: audioURL) {
        body.appendFileField("audio", filename: "instruction.m4a", contentType: "audio/mp4", data: audioData, boundary: boundary)
      }
      body.append("--\(boundary)--\r\n")

      var request = URLRequest(url: url)
      request.httpMethod = "POST"
      request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
      if let key = Bundle.main.object(forInfoDictionaryKey: "CaptureBridgeAPIKey") as? String,
        !key.isEmpty
      {
        request.setValue(key, forHTTPHeaderField: "X-Capture-Key")
      }
      let (data, response) = try await URLSession.shared.upload(for: request, from: body)
      guard let http = response as? HTTPURLResponse, http.statusCode == 201 else {
        let message = String(data: data, encoding: .utf8) ?? "unknown response"
        throw NSError(domain: "CaptureBridge", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
      }
      let manifest = try JSONDecoder().decode(CaptureManifestResponse.self, from: data)
      lastCaptureId = manifest.captureId
      status = "Exposed capture \(manifest.captureId.prefix(8))"
      cleanupAudio()
    } catch {
      status = "Upload failed: \(error.localizedDescription)"
    }
  }

  private func jsonString(_ object: [String: Any]) throws -> String {
    let data = try JSONSerialization.data(withJSONObject: object, options: [])
    return String(decoding: data, as: UTF8.self)
  }

  private func cleanupAudio() {
    if let audioURL { try? FileManager.default.removeItem(at: audioURL) }
    audioURL = nil
  }
}

private extension Data {
  mutating func append(_ string: String) {
    append(Data(string.utf8))
  }

  mutating func appendFormField(_ name: String, value: String, boundary: String) {
    append("--\(boundary)\r\n")
    append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
    append("\(value)\r\n")
  }

  mutating func appendFileField(
    _ name: String,
    filename: String,
    contentType: String,
    data: Data,
    boundary: String
  ) {
    append("--\(boundary)\r\n")
    append("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n")
    append("Content-Type: \(contentType)\r\n\r\n")
    append(data)
    append("\r\n")
  }
}
