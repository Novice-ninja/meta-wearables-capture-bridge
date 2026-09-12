import AVFoundation
import CoreLocation
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
/// uploads it with the next DAT photo or recorded video. When Ray-Ban audio is available as a
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
  private let locationCollector = LocationMetadataCollector()

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
    await upload(imageData: photoData, videoData: nil)
  }

  func upload(videoURL: URL) async {
    do {
      await upload(imageData: nil, videoData: try Data(contentsOf: videoURL))
    } catch {
      status = "Couldn't read recorded video: \(error.localizedDescription)"
    }
  }

  private func upload(imageData: Data?, videoData: Data?) async {
    if isRecordingInstruction { stopInstructionRecording() }
    guard imageData != nil || videoData != nil else {
      status = "Nothing to upload"
      return
    }
    guard let baseURLText = Bundle.main.object(forInfoDictionaryKey: "CaptureBridgeURL") as? String,
      !baseURLText.contains("$("),
      let baseURL = URL(string: baseURLText),
      let scheme = baseURL.scheme,
      ["http", "https"].contains(scheme),
      baseURL.host != nil
    else {
      status = "Set CAPTURE_BRIDGE_URL in Xcode Build Settings"
      return
    }
    let url = baseURL.appendingPathComponent("v1/captures")

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
        "capture_kind": imageData == nil ? "video" : "photo",
        "audio_input": recordedAudioInputName,
        "audio_input_type": recordedAudioInputType,
      ]
      if imageData != nil {
        metadata["image_origin"] = "MWDATCamera.PhotoData"
      }
      metadata["phone_model"] = UIDevice.current.model
      if let location = locationCollector.snapshot() {
        metadata["location"] = location
      }

      let boundary = "CaptureBridge-\(UUID().uuidString)"
      var body = Data()
      body.appendFormField("captured_at", value: ISO8601DateFormatter().string(from: Date()), boundary: boundary)
      body.appendFormField("source_json", value: try jsonString(source), boundary: boundary)
      body.appendFormField("metadata_json", value: try jsonString(metadata), boundary: boundary)
      if !instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
        body.appendFormField("instruction", value: instruction, boundary: boundary)
      }
      if let imageData {
        body.appendFileField(
          "image", filename: "capture.jpg", contentType: "image/jpeg", data: imageData, boundary: boundary
        )
      }
      if let videoData {
        body.appendFileField(
          "video", filename: "capture.mp4", contentType: "video/mp4", data: videoData, boundary: boundary
        )
      }
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

/// Captures a best-effort current iPhone location for the media manifest. It
/// never blocks capture/upload: the first call requests permission and later
/// captures include the most recently delivered location when permission is on.
private final class LocationMetadataCollector: NSObject, CLLocationManagerDelegate {
  private let manager = CLLocationManager()
  private var latestLocation: CLLocation?

  override init() {
    super.init()
    manager.delegate = self
    manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
  }

  func snapshot() -> [String: Any]? {
    guard CLLocationManager.locationServicesEnabled() else { return nil }
    switch manager.authorizationStatus {
    case .notDetermined:
      manager.requestWhenInUseAuthorization()
    case .authorizedAlways, .authorizedWhenInUse:
      manager.requestLocation()
    case .denied, .restricted:
      break
    @unknown default:
      break
    }
    guard let location = latestLocation else { return nil }
    return [
      "latitude": location.coordinate.latitude,
      "longitude": location.coordinate.longitude,
      "horizontal_accuracy_meters": location.horizontalAccuracy,
      "captured_at": ISO8601DateFormatter().string(from: location.timestamp),
    ]
  }

  func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
    latestLocation = locations.last
  }

  func locationManager(_ manager: CLLocationManager, didChangeAuthorization status: CLAuthorizationStatus) {
    if status == .authorizedAlways || status == .authorizedWhenInUse {
      manager.requestLocation()
    }
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
