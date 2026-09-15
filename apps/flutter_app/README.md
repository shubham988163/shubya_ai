# Shubya AI - Flutter Multiplatform (Android & Web)

This directory contains the cross-platform frontend for Shubya AI, built with Flutter to target:
1. **Android Mobile App** (Smartphones & Tablets)
2. **Web Dashboard** (Laptops & Desktop browsers)

## Features Included
- 🚀 **Market Dashboard**: Live index trackers, AI market radar, and F&O high probability setups.
- 📊 **Options Chain**: Real-time Open Interest (OI), strike selection, and call/put LTP matrix.
- 🔔 **Signal Alerts & Telegram Logs**: Instant breakdown/breakout notifications.
- 🎨 **Dark Modern Trading UI**: Tailored slate and neon accents.

## How to Run & Build

### Prerequisites
Install [Flutter SDK](https://docs.flutter.dev/get-started/install/windows) (version 3.0+).

### 1. Run on Android Mobile:
```bash
cd apps/flutter_app
flutter pub get
flutter run -d android
```

### 2. Run on Web Browser (Laptop/Desktop):
```bash
cd apps/flutter_app
flutter pub get
flutter run -d chrome
```

### 3. Build Production Releases:
- **Android APK**: `flutter build apk --release`
- **Android App Bundle (Google Play)**: `flutter build appbundle --release`
- **Web App (Production hosting)**: `flutter build web --release`
