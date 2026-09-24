from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Optional
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Surplus to Shelter", description="Time-sensitive food rescue coordination API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class DonationCreate(BaseModel):
    food_item: str = Field(min_length=2, max_length=100)
    category: str = "Prepared meals"
    quantity_kg: float = Field(gt=0, le=5000)
    servings: int = Field(gt=0, le=10000)
    donor_name: str = Field(min_length=2, max_length=100)
    donor_location: str = Field(min_length=2, max_length=200)
    donor_lat: float = Field(ge=-90, le=90)
    donor_lng: float = Field(ge=-180, le=180)
    pickup_notes: str = ""
    allergen_info: str = ""
    temperature: str = "Ambient"
    hours_until_expiry: float = Field(gt=0, le=168)
    safety_confirmed: bool


class StatusUpdate(BaseModel):
    status: str


class CapacityUpdate(BaseModel):
    capacity_kg: float = Field(ge=0, le=10000)
    needs: list[str] = Field(default_factory=list)


def utcnow():
    return datetime.now(timezone.utc)


def distance_km(lat1, lon1, lat2, lon2):
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371 * 2 * asin(sqrt(a))


shelters = [
    {"id": "S1", "name": "Hope Community Kitchen", "location": "Downtown", "lat": 12.9716, "lng": 77.5946,
     "capacity_kg": 70, "needs": ["Prepared meals", "Produce", "Bakery"], "contact": "Anita · 9:00–21:00"},
    {"id": "S2", "name": "Safe Haven Shelter", "location": "Indiranagar", "lat": 12.9784, "lng": 77.6408,
     "capacity_kg": 28, "needs": ["Prepared meals", "Dairy", "Bakery"], "contact": "Ravi · 24 hours"},
    {"id": "S3", "name": "Northside Food Bank", "location": "Hebbal", "lat": 13.0358, "lng": 77.5970,
     "capacity_kg": 45, "needs": ["Produce", "Bakery", "Packaged"], "contact": "Meera · 8:00–18:00"},
]
donations = {}


@app.get("/health")
def health():
    return {"status": "ok", "service": "Surplus to Shelter"}


@app.get("/shelters")
def get_shelters():
    return shelters


@app.patch("/shelters/{shelter_id}")
def update_shelter(shelter_id: str, update: CapacityUpdate):
    shelter = next((s for s in shelters if s["id"] == shelter_id), None)
    if not shelter:
        raise HTTPException(404, "Recipient not found")
    shelter["capacity_kg"] = update.capacity_kg
    shelter["needs"] = update.needs
    return shelter


@app.post("/donations")
def create_donation(payload: DonationCreate):
    if not payload.safety_confirmed:
        raise HTTPException(400, "Please confirm the food is safe to donate and was handled appropriately.")
    if payload.hours_until_expiry < 1:
        raise HTTPException(400, "Food with less than one hour in its safe-use window cannot be listed.")
    donation = payload.model_dump()
    donation.update({"id": uuid.uuid4().hex[:8].upper(), "created_at": utcnow().isoformat(),
                     "expiry_time": (utcnow() + timedelta(hours=payload.hours_until_expiry)).isoformat(),
                     "status": "AVAILABLE", "matched_shelter": None, "distance_km": None,
                     "match_reason": None, "driver_name": None})
    donations[donation["id"]] = donation
    return donation


@app.get("/donations")
def get_donations():
    # Expiry is enforced on reads as well as matching to prevent stale offers being dispatched.
    for item in donations.values():
        if item["status"] == "AVAILABLE" and datetime.fromisoformat(item["expiry_time"]) <= utcnow():
            item["status"] = "EXPIRED"
    return sorted(donations.values(), key=lambda d: d["created_at"], reverse=True)


@app.post("/match/{donation_id}")
def match_donation(donation_id: str):
    item = donations.get(donation_id)
    if not item:
        raise HTTPException(404, "Donation not found")
    if item["status"] != "AVAILABLE":
        raise HTTPException(400, f"Donation is {item['status'].lower()} and cannot be matched")
    remaining = (datetime.fromisoformat(item["expiry_time"]) - utcnow()).total_seconds() / 3600
    if remaining <= 0:
        item["status"] = "EXPIRED"
        raise HTTPException(400, "Food safety window expired")
    candidates = []
    for shelter in shelters:
        if shelter["capacity_kg"] < item["quantity_kg"]:
            continue
        km = distance_km(item["donor_lat"], item["donor_lng"], shelter["lat"], shelter["lng"])
        need_bonus = 1 if item["category"] in shelter["needs"] else 0
        # Prioritize a recipient that needs this food, then proximity and capacity fit.
        score = need_bonus * 60 - km * 2 - (shelter["capacity_kg"] - item["quantity_kg"]) * 0.1
        candidates.append((score, shelter, km, need_bonus))
    if not candidates:
        raise HTTPException(409, "No recipient currently has enough capacity. Try again after capacity is updated.")
    _, shelter, km, need_bonus = max(candidates, key=lambda c: c[0])
    item.update({"status": "MATCHED", "matched_shelter": shelter["name"], "shelter_id": shelter["id"],
                 "distance_km": round(km, 1),
                 "match_reason": f"{shelter['name']} has {shelter['capacity_kg']} kg free, is {km:.1f} km away, "
                                 + ("and lists this category as a current need." if need_bonus else "and can accept this quantity.")})
    shelter["capacity_kg"] -= item["quantity_kg"]
    return {"donation": item, "recipient": shelter, "alternatives": [
        {"name": s["name"], "distance_km": round(d, 1), "capacity_kg": s["capacity_kg"]}
        for _, s, d, _ in sorted(candidates, key=lambda c: c[0], reverse=True) if s["id"] != shelter["id"]]}


@app.patch("/donations/{donation_id}/status")
def update_status(donation_id: str, update: StatusUpdate):
    item = donations.get(donation_id)
    if not item:
        raise HTTPException(404, "Donation not found")
    transitions = {"MATCHED": "PICKED_UP", "PICKED_UP": "DELIVERED"}
    if transitions.get(item["status"]) != update.status:
        raise HTTPException(400, f"Cannot change {item['status']} to {update.status}")
    if update.status == "PICKED_UP" and datetime.fromisoformat(item["expiry_time"]) <= utcnow():
        raise HTTPException(400, "Food safety window expired before pickup")
    item["status"] = update.status
    item["updated_at"] = utcnow().isoformat()
    return item


@app.get("/impact")
def impact():
    delivered = [d for d in donations.values() if d["status"] == "DELIVERED"]
    rescued_kg = sum(d["quantity_kg"] for d in delivered)
    meals = sum(d["servings"] for d in delivered)
    return {"meals_rescued": meals, "food_diverted_kg": round(rescued_kg, 1),
            "co2e_avoided_kg": round(rescued_kg * 2.5, 1),
            "completed_deliveries": len(delivered), "active_rescues": sum(d["status"] in ("MATCHED", "PICKED_UP") for d in donations.values()),
            "total_listings": len(donations)}

