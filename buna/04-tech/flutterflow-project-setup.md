# إعداد مشروع FlutterFlow — تطبيق بُنة

هذا الدليل يشرح **كيفية إعداد مشروع بُنة في FlutterFlow** خطوة بخطوة، بهيكل قابل للتوسع لدول الخليج من اليوم الأول.

---

## 1. إنشاء المشروع

### المعلومات الأساسية
| الحقل | القيمة |
|-------|--------|
| Project Name | `buna-app` |
| Display Name | `بُنة` |
| Bundle ID (iOS) | `app.buna.ios` |
| Package Name (Android) | `app.buna.android` |
| Primary Color | `#6F4E37` (Roast) |
| Default Language | `Arabic (ar)` |
| Supported Languages | `Arabic (ar)`, `English (en)` |
| Direction | `RTL primary, LTR secondary` |

### الإعدادات المتقدمة
- ✅ Enable Authentication
- ✅ Enable Firebase
- ✅ Enable Push Notifications (FCM)
- ✅ Enable Localization
- ✅ Enable Dark Mode (اختياري للمرحلة 2)
- ✅ Enable Deep Links

---

## 2. هيكل قاعدة البيانات (Firestore Schema)

### مجموعة `users`

```json
{
  "uid": "string (Auth ID)",
  "display_name": "string",
  "username": "string (unique)",
  "email": "string",
  "phone": "string (optional)",
  "photo_url": "string (Firebase Storage URL)",
  "bio": "string (max 160 chars)",
  
  "country": "string (KW|SA|AE|QA|BH|OM)",
  "city": "string",
  "preferred_language": "string (ar|en)",
  
  "is_admin": "boolean (default: false)",
  "is_verified": "boolean (default: false)",
  "is_banned": "boolean (default: false)",
  
  "favorites": "array<DocumentReference>",
  "visited_cafes": "array<DocumentReference>",
  "visit_count": "number (default: 0)",
  "review_count": "number (default: 0)",
  "passport_level": "number (default: 1)",
  
  "preferences": {
    "espresso_lover": "boolean",
    "v60_lover": "boolean",
    "sweet_lover": "boolean",
    "work_friendly_seeker": "boolean",
    "preferred_milk": "string (whole|oat|almond|none)"
  },
  
  "fcm_token": "string",
  "notification_settings": {
    "geofence_alerts": "boolean (default: true)",
    "new_cafe_alerts": "boolean (default: true)",
    "review_responses": "boolean (default: true)",
    "marketing": "boolean (default: false)"
  },
  
  "created_at": "timestamp",
  "last_active_at": "timestamp"
}
```

### مجموعة `cafes`

```json
{
  "id": "string (auto)",
  "name_ar": "string",
  "name_en": "string",
  "slug": "string (unique, e.g. vol-1-kuwait)",
  
  "country": "string (KW|SA|AE|QA|BH|OM)",
  "city": "string",
  "region": "string",
  "address_ar": "string",
  "address_en": "string",
  "location": "geopoint",
  "geohash": "string (for radius queries)",
  
  "description_ar": "string",
  "description_en": "string",
  
  "main_image": "string (Storage URL)",
  "gallery": "array<string>",
  "logo": "string",
  
  "phone": "string",
  "whatsapp": "string",
  "instagram": "string",
  "website": "string",
  "google_maps_link": "string",
  
  "operating_hours": {
    "monday": {"open": "07:00", "close": "23:00", "closed": false},
    "tuesday": {"open": "07:00", "close": "23:00", "closed": false},
    "wednesday": {"open": "07:00", "close": "23:00", "closed": false},
    "thursday": {"open": "07:00", "close": "23:00", "closed": false},
    "friday": {"open": "14:00", "close": "00:00", "closed": false},
    "saturday": {"open": "07:00", "close": "23:00", "closed": false},
    "sunday": {"open": "07:00", "close": "23:00", "closed": false}
  },
  
  "equipment_tags": "array<string> (V60|Espresso|Chemex|AeroPress|ColdBrew|Syphon|Kalita)",
  "machines": "array<string> (LaMarzocco|Slayer|Synesso|VictoriaArduino)",
  "milk_options": "array<string> (whole|oat|almond|soy|coconut|lactose-free)",
  
  "roaster_refs": "array<DocumentReference>",
  "current_beans": "array<{origin, process, roaster_ref, roast_date}>",
  
  "amenities": {
    "wifi": "boolean",
    "wifi_speed_mbps": "number",
    "power_outlets": "boolean",
    "outdoor_seating": "boolean",
    "indoor_seating": "boolean",
    "parking": "boolean",
    "valet": "boolean",
    "kids_friendly": "boolean",
    "pets_allowed": "boolean",
    "wheelchair_accessible": "boolean",
    "prayer_area": "boolean",
    "smoking_area": "boolean"
  },
  
  "price_range": "number (1-4, like $$$)",
  "average_price": {
    "amount": "number",
    "currency": "string (KWD|SAR|AED|QAR|BHD|OMR)"
  },
  
  "specialty_scores": {
    "espresso_votes": "number (default: 0)",
    "v60_votes": "number (default: 0)",
    "sweets_votes": "number (default: 0)",
    "workspace_votes": "number (default: 0)",
    "community_votes": "number (default: 0)"
  },
  
  "active_badges": "array<string> (Espresso|V60|Sweets|Workspace|Community)",
  
  "ratings": {
    "overall": "number (0-5, calculated)",
    "coffee": "number",
    "service": "number",
    "ambiance": "number",
    "value": "number",
    "workspace": "number"
  },
  "total_reviews": "number (default: 0)",
  "total_visits": "number (default: 0)",
  
  "is_featured": "boolean (default: false)",
  "is_verified": "boolean (default: false)",
  "is_partner": "boolean (default: false)",
  "is_active": "boolean (default: true)",
  "claimed_by": "DocumentReference (user) | null",
  
  "tags": "array<string>",
  "open_year": "number",
  
  "created_at": "timestamp",
  "updated_at": "timestamp",
  "last_inspected_at": "timestamp"
}
```

### مجموعة `roasters`

```json
{
  "id": "string",
  "name_ar": "string",
  "name_en": "string",
  "slug": "string",
  
  "country": "string",
  "city": "string",
  "founded_year": "number",
  
  "logo": "string",
  "cover_image": "string",
  
  "description_ar": "string",
  "description_en": "string",
  "philosophy": "string",
  
  "specialties": "array<string> (light_roast|medium|dark|espresso_blend)",
  "certifications": "array<string> (SCA|Q-Grader|Fair-Trade|Organic)",
  
  "website": "string",
  "instagram": "string",
  "shop_url": "string (online store)",
  
  "supplies_to": "array<DocumentReference> (cafes)",
  
  "is_local": "boolean",
  "is_active": "boolean",
  
  "created_at": "timestamp"
}
```

### مجموعة `reviews`

```json
{
  "id": "string",
  "user_ref": "DocumentReference",
  "cafe_ref": "DocumentReference",
  
  "ratings": {
    "coffee": "number (1-5)",
    "service": "number (1-5)",
    "ambiance": "number (1-5)",
    "workspace": "number (1-5)",
    "value": "number (1-5)"
  },
  "overall_rating": "number (calculated avg)",
  
  "title": "string (optional)",
  "comment": "string",
  "language": "string (ar|en)",
  
  "recommend_espresso": "boolean",
  "recommend_v60": "boolean",
  "recommend_sweets": "boolean",
  "recommend_workspace": "boolean",
  
  "drinks_ordered": "array<string>",
  "visit_purpose": "string (work|social|quick|date|study)",
  "visit_time": "string (morning|afternoon|evening|night)",
  
  "photos": "array<string>",
  
  "helpful_count": "number (default: 0)",
  "report_count": "number (default: 0)",
  
  "is_verified_visit": "boolean",
  "is_hidden": "boolean (moderation)",
  
  "cafe_response": {
    "text": "string",
    "responded_at": "timestamp",
    "responded_by": "DocumentReference"
  },
  
  "created_at": "timestamp",
  "updated_at": "timestamp"
}
```

### مجموعة `cafe_submissions`

```json
{
  "id": "string",
  "cafe_name": "string",
  "google_maps_link": "string",
  "phone": "string",
  "instagram": "string",
  "city": "string",
  "country": "string",
  "notes": "string",
  
  "submitted_by": "DocumentReference",
  "status": "string (pending|under_review|approved|rejected)",
  "rejection_reason": "string",
  
  "reviewed_by": "DocumentReference",
  "reviewed_at": "timestamp",
  
  "approved_cafe_ref": "DocumentReference (when approved)",
  
  "created_at": "timestamp"
}
```

### مجموعة `user_visits`

```json
{
  "id": "string",
  "user_ref": "DocumentReference",
  "cafe_ref": "DocumentReference",
  "visited_at": "timestamp",
  "verification_method": "string (geofence|qr|manual)",
  "review_ref": "DocumentReference (optional)"
}
```

### مجموعة `city_guides`

```json
{
  "id": "string",
  "title_ar": "string",
  "title_en": "string",
  "slug": "string",
  
  "city": "string",
  "country": "string",
  
  "description_ar": "string",
  "description_en": "string",
  "cover_image": "string",
  
  "cafes": "array<DocumentReference>",
  "duration": "string (half_day|full_day|weekend)",
  "type": "string (work|date|tour|sweet_tooth|espresso_lover)",
  
  "author_ref": "DocumentReference",
  "is_published": "boolean",
  "view_count": "number",
  
  "created_at": "timestamp",
  "updated_at": "timestamp"
}
```

### مجموعة `events`

```json
{
  "id": "string",
  "title_ar": "string",
  "title_en": "string",
  
  "type": "string (cupping|workshop|latte_art|competition|guest_roaster)",
  "cafe_ref": "DocumentReference",
  
  "description_ar": "string",
  "description_en": "string",
  "cover_image": "string",
  
  "starts_at": "timestamp",
  "ends_at": "timestamp",
  
  "price": {
    "amount": "number",
    "currency": "string"
  },
  "capacity": "number",
  "registered_count": "number",
  
  "registration_link": "string",
  
  "is_free": "boolean",
  "is_published": "boolean",
  
  "created_at": "timestamp"
}
```

---

## 3. هيكل الشاشات (App Pages)

### المجموعة الأولى: التصفح (Browse)
```
HomePage                     — الخريطة + قائمة المقاهي
├── CafeDetailsPage          — تفاصيل المقهى
│   ├── ReviewsListPage
│   │   └── ReviewDetailPage
│   ├── PhotosGalleryPage
│   ├── WriteReviewPage
│   └── ShareCafeSheet
├── SearchPage               — البحث المتقدم
├── FilterPage               — الفلترة
├── CityGuidesListPage       — أدلة المدن
│   └── CityGuideDetailPage
└── EventsListPage           — الفعاليات
    └── EventDetailPage
```

### المجموعة الثانية: المستخدم (User)
```
ProfilePage                  — الصفحة الشخصية
├── CoffeePassportPage       — جواز القهوة
├── FavoritesPage            — المفضلة
├── MyReviewsPage            — تقييماتي
├── EditProfilePage
└── SettingsPage
    ├── NotificationsPage
    ├── LanguagePage
    ├── PrivacyPage
    └── AboutPage
```

### المجموعة الثالثة: المصادقة (Auth)
```
SplashPage
├── OnboardingPage           — 3-4 شاشات تعريفية
├── LoginPage
│   ├── EmailLoginPage
│   ├── PhoneLoginPage
│   └── (Apple, Google buttons)
├── RegisterPage
└── ForgotPasswordPage
```

### المجموعة الرابعة: الإدارة (Admin — مخفي)
```
AdminDashboardPage           — صلاحية is_admin = true فقط
├── PendingSubmissionsPage   — طلبات المقاهي
├── AddCafePage              — إضافة مقهى
├── EditCafePage
├── ManageRoastersPage
├── ModerationQueuePage      — مراجعة التقييمات
├── UsersListPage
└── AnalyticsPage
```

### المجموعة الخامسة: الشركاء (Partner — للمرحلة 2)
```
PartnerDashboardPage         — صلاحية claimed_by = user
├── EditCafeInfoPage
├── RespondToReviewsPage
├── PartnerAnalyticsPage
└── EventManagementPage
```

---

## 4. App States (المتغيرات العامة)

```dart
// User State
currentUser: DocumentReference
isAdmin: bool
isLoggedIn: bool
preferredLanguage: String
currentLocation: LatLng

// Cache State
cachedCafes: List<DocumentReference>
lastFetchTime: DateTime
selectedCity: String
selectedCountry: String

// UI State
selectedFilters: List<String>
mapZoomLevel: double
isMapView: bool (toggle map/list)
isDarkMode: bool
```

---

## 5. Custom Functions الأساسية

### 5.1 حساب الأوسمة
```dart
bool shouldShowBadge(int votes, int totalReviews) {
  if (totalReviews < 15) return false;
  return (votes / totalReviews) * 100 >= 70;
}
```

### 5.2 حالة المقهى (مفتوح/مغلق)
```dart
String getCafeStatus(Map<String, dynamic> hours) {
  final now = DateTime.now();
  final dayKey = ['sunday', 'monday', 'tuesday', 'wednesday', 
                  'thursday', 'friday', 'saturday'][now.weekday % 7];
  final today = hours[dayKey];
  
  if (today == null || today['closed'] == true) return 'closed';
  
  final openTime = _parseTime(today['open']);
  final closeTime = _parseTime(today['close']);
  final currentMinutes = now.hour * 60 + now.minute;
  
  if (currentMinutes < openTime || currentMinutes > closeTime) {
    return 'closed';
  }
  
  if (closeTime - currentMinutes <= 30) return 'closing_soon';
  return 'open';
}
```

### 5.3 حساب المسافة بين المستخدم والمقهى
```dart
double calculateDistance(LatLng userLoc, LatLng cafeLoc) {
  // Haversine formula
  const earthRadius = 6371; // km
  final dLat = _toRadians(cafeLoc.latitude - userLoc.latitude);
  final dLon = _toRadians(cafeLoc.longitude - userLoc.longitude);
  
  final a = sin(dLat/2) * sin(dLat/2) +
            cos(_toRadians(userLoc.latitude)) * 
            cos(_toRadians(cafeLoc.latitude)) *
            sin(dLon/2) * sin(dLon/2);
  
  final c = 2 * atan2(sqrt(a), sqrt(1-a));
  return earthRadius * c;
}
```

### 5.4 صياغة السعر بالعملة المحلية
```dart
String formatPrice(double amount, String currency) {
  final symbols = {
    'KWD': 'د.ك',
    'SAR': 'ر.س',
    'AED': 'د.إ',
    'QAR': 'ر.ق',
    'BHD': 'د.ب',
    'OMR': 'ر.ع'
  };
  return '${amount.toStringAsFixed(3)} ${symbols[currency] ?? currency}';
}
```

---

## 6. Custom Actions الأساسية

### 6.1 المشاركة (Share)
موجود بالفعل في المحادثة السابقة — `shareCafeDetails()`

### 6.2 فتح خرائط جوجل
استخدام Action مدمج: `Launch Map`

### 6.3 طلب صلاحية الموقع
```dart
import 'package:geolocator/geolocator.dart';

Future<bool> requestLocationPermission() async {
  LocationPermission permission = await Geolocator.checkPermission();
  if (permission == LocationPermission.denied) {
    permission = await Geolocator.requestPermission();
  }
  return permission == LocationPermission.always || 
         permission == LocationPermission.whileInUse;
}
```

### 6.4 رفع صورة مع ضغط
```dart
import 'package:image_picker/image_picker.dart';
import 'package:flutter_image_compress/flutter_image_compress.dart';

Future<Uint8List?> pickAndCompressImage() async {
  final picker = ImagePicker();
  final image = await picker.pickImage(source: ImageSource.gallery);
  if (image == null) return null;
  
  final bytes = await image.readAsBytes();
  final compressed = await FlutterImageCompress.compressWithList(
    bytes,
    quality: 80,
    minWidth: 1080,
  );
  return compressed;
}
```

---

## 7. Firestore Security Rules

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    
    // Helper functions
    function isAuthenticated() {
      return request.auth != null;
    }
    
    function isAdmin() {
      return isAuthenticated() && 
             get(/databases/$(database)/documents/users/$(request.auth.uid)).data.is_admin == true;
    }
    
    function isOwner(userId) {
      return isAuthenticated() && request.auth.uid == userId;
    }
    
    // Users collection
    match /users/{userId} {
      allow read: if isAuthenticated();
      allow create: if isOwner(userId);
      allow update: if isOwner(userId) || isAdmin();
      allow delete: if isAdmin();
    }
    
    // Cafes collection
    match /cafes/{cafeId} {
      allow read: if true; // public
      allow create, update, delete: if isAdmin();
    }
    
    // Roasters collection
    match /roasters/{roasterId} {
      allow read: if true;
      allow create, update, delete: if isAdmin();
    }
    
    // Reviews collection
    match /reviews/{reviewId} {
      allow read: if true;
      allow create: if isAuthenticated();
      allow update: if isOwner(resource.data.user_ref.id) || isAdmin();
      allow delete: if isOwner(resource.data.user_ref.id) || isAdmin();
    }
    
    // Cafe submissions
    match /cafe_submissions/{submissionId} {
      allow create: if isAuthenticated();
      allow read: if isAdmin() || isOwner(resource.data.submitted_by.id);
      allow update, delete: if isAdmin();
    }
    
    // User visits (private)
    match /user_visits/{visitId} {
      allow read, write: if isOwner(resource.data.user_ref.id);
    }
    
    // City guides
    match /city_guides/{guideId} {
      allow read: if resource.data.is_published == true;
      allow create, update, delete: if isAdmin();
    }
    
    // Events
    match /events/{eventId} {
      allow read: if resource.data.is_published == true;
      allow create, update, delete: if isAdmin();
    }
  }
}
```

---

## 8. Firebase Storage Rules

```javascript
rules_version = '2';
service firebase.storage {
  match /b/{bucket}/o {
    
    // User profile photos
    match /users/{userId}/profile/{fileName} {
      allow read: if true;
      allow write: if request.auth.uid == userId
                   && request.resource.size < 5 * 1024 * 1024
                   && request.resource.contentType.matches('image/.*');
    }
    
    // Cafe photos (admin only)
    match /cafes/{cafeId}/{fileName} {
      allow read: if true;
      allow write: if request.auth != null
                   && firestore.get(/databases/(default)/documents/users/$(request.auth.uid)).data.is_admin == true;
    }
    
    // Review photos
    match /reviews/{userId}/{fileName} {
      allow read: if true;
      allow write: if request.auth.uid == userId
                   && request.resource.size < 10 * 1024 * 1024;
    }
  }
}
```

---

## 9. Cloud Functions المطلوبة

### 9.1 تحديث متوسط التقييم
```javascript
// عند إضافة/تعديل/حذف تقييم → إعادة حساب متوسط المقهى
exports.updateCafeRating = functions.firestore
  .document('reviews/{reviewId}')
  .onWrite(async (change, context) => {
    // ... logic to recalculate cafe's average ratings
  });
```

### 9.2 تحديث الأوسمة تلقائياً
```javascript
exports.updateCafeBadges = functions.firestore
  .document('reviews/{reviewId}')
  .onWrite(async (change, context) => {
    // ... logic to add/remove badges based on votes
  });
```

### 9.3 إشعارات Geofencing (المرحلة 2)
```javascript
exports.sendNearbyCafeAlert = functions.https.onCall(...);
```

### 9.4 موافقة طلب مقهى
```javascript
exports.approveCafeSubmission = functions.https.onCall(async (data, context) => {
  // ... copy submission to cafes collection
});
```

---

## 10. التكامل مع الخدمات الخارجية

| الخدمة | الغرض | التكلفة |
|--------|------|---------|
| Google Maps API | الخرائط، Directions | $200 مجاني/شهر |
| Algolia | البحث المتقدم | مجاني حتى 10K records |
| OneSignal أو FCM | Push notifications | مجاني |
| Sentry | تتبع الأخطاء | مجاني للمشاريع الصغيرة |
| Mixpanel أو Amplitude | Analytics | مجاني حتى 100K events |
| RevenueCat | الاشتراكات (مرحلة 2) | مجاني حتى $10K MRR |

---

## 11. ترتيب البناء الموصى به (Build Order)

### Sprint 1 (أسبوع 1-2): الأساس
- [ ] إعداد المشروع + Firebase
- [ ] قاعدة البيانات (Schema)
- [ ] Authentication (Email + Apple + Google)
- [ ] Splash + Onboarding
- [ ] Profile Page (basic)

### Sprint 2 (أسبوع 3-4): الاكتشاف
- [ ] Home Page (Map + List)
- [ ] Cafe Details Page
- [ ] Search & Filter
- [ ] Favorites

### Sprint 3 (أسبوع 5-6): المراجعات
- [ ] Write Review Page
- [ ] Reviews List
- [ ] Coffee Passport
- [ ] Sharing

### Sprint 4 (أسبوع 7): الإدارة
- [ ] Admin Dashboard
- [ ] Add/Edit Cafe
- [ ] Submission Review

### Sprint 5 (أسبوع 8): التحسين والاختبار
- [ ] Localization (ar/en)
- [ ] Empty States
- [ ] Loading States
- [ ] Error Handling
- [ ] Beta Testing

---

**آخر تحديث:** 2026-04-14
