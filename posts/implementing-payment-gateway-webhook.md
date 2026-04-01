---
title: "Django Payment Gateways: Webhooks, Idempotency, and Async Recon Done Right"
date: 2025-08-01
author: Aniket Maithani
tags: [django, payments, razorpay, stripe, fintech, celery, security]
description: "Most Django payment integrations handle the happy path well. This post covers what actually breaks in production: webhook idempotency, signature verification, and async reconciliation."
reading_time: 10
status: published
---

## Why Most Payment Integrations Are Incomplete

I have integrated Razorpay, Stripe, Cashfree, and Augmont Gold across multiple products. PlusGold at GetPlus, the Synapse Conclave registration platform, and a few client projects in between. One thing I have noticed consistently is that developers spend most of their time on the payment initiation flow and almost no time on what happens after the payment completes.

That is the wrong place to spend your energy.

The payment initiation is largely a form submission with an API call. The real complexity lives in three areas: webhook handling, idempotency, and reconciliation. Get these wrong and you will double-credit accounts, miss failed transactions, and have no audit trail when finance comes asking.

This post covers all three, with production-grade Django patterns for each.

---

## The Basics: A Clean Gateway Abstraction

Before getting into webhooks, the foundation matters. If you scatter Razorpay-specific code across your views, you will regret it the first time you add a second gateway.

Start with an abstract base class:

```python
# payments/gateways/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PaymentOrder:
    gateway_order_id: str
    amount: Decimal
    currency: str
    metadata: dict


@dataclass
class PaymentResult:
    success: bool
    gateway_payment_id: str
    gateway_order_id: str
    amount: Decimal
    currency: str
    raw_response: dict


class PaymentGateway(ABC):

    @abstractmethod
    def create_order(self, amount: Decimal, currency: str, metadata: dict) -> PaymentOrder:
        pass

    @abstractmethod
    def verify_signature(self, payload: dict, signature: str) -> bool:
        pass

    @abstractmethod
    def fetch_payment(self, payment_id: str) -> PaymentResult:
        pass

    @abstractmethod
    def refund(self, payment_id: str, amount: Decimal) -> dict:
        pass
```

Both your Razorpay and Stripe implementations inherit from this. Your business logic calls `gateway.create_order()` and never imports `razorpay` directly. When you add a third gateway, nothing else changes.

---

## Webhooks: What the Docs Do Not Tell You

Every payment gateway sends webhooks. Razorpay retries up to 5 times over 24 hours. Stripe retries for 3 days. If your webhook handler is not idempotent, you will process the same payment event multiple times.

Here is what that looks like in a real database model:

```python
# payments/models.py
from django.db import models
from django.utils import timezone


class WebhookEvent(models.Model):
    GATEWAY_CHOICES = [
        ('razorpay', 'Razorpay'),
        ('stripe', 'Stripe'),
        ('cashfree', 'Cashfree'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    ]

    event_id = models.CharField(max_length=255)
    gateway = models.CharField(max_length=50, choices=GATEWAY_CHOICES)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    raw_body = models.TextField()  # store raw bytes as text for signature re-verification
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('event_id', 'gateway')]
        indexes = [
            models.Index(fields=['gateway', 'status']),
            models.Index(fields=['event_id', 'gateway']),
        ]

    def __str__(self):
        return f"{self.gateway}:{self.event_id} [{self.status}]"
```

The `unique_together` on `(event_id, gateway)` is your database-level idempotency guard. Even if your application code has a race condition, the DB constraint prevents duplicate processing.

The `raw_body` field is important. You need the raw request body, not the parsed JSON, for signature verification. More on this below.

---

## Signature Verification: Do Not Skip This

Signature verification is non-negotiable. Without it, anyone on the internet can POST a fake `payment.captured` event to your webhook endpoint and credit an account without any actual payment.

Both Razorpay and Stripe sign their webhook payloads using HMAC-SHA256. The verification differs slightly between gateways.

**Razorpay:**

```python
import hmac
import hashlib


def verify_razorpay_signature(webhook_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode('utf-8'),
        webhook_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Razorpay sends the signature in the `X-Razorpay-Signature` header. The body must be the raw bytes from the request, not the re-serialized JSON. This is why you store `raw_body` on the model.

**Stripe:**

```python
import stripe
from django.conf import settings


def verify_stripe_signature(webhook_body: bytes, signature: str) -> stripe.Event:
    # Stripe's SDK handles timestamp tolerance (prevents replay attacks)
    return stripe.Webhook.construct_event(
        webhook_body,
        signature,
        settings.STRIPE_WEBHOOK_SECRET
    )
```

Stripe's verification also includes a timestamp tolerance check (default 300 seconds) which protects against replay attacks. Razorpay does not do this out of the box, so be aware.

**The webhook view, with verification baked in:**

```python
# payments/views.py
import json
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import HttpResponse, HttpResponseBadRequest
from .models import WebhookEvent
from .tasks import process_webhook_event


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    signature = request.headers.get('X-Razorpay-Signature', '')
    raw_body = request.body

    if not verify_razorpay_signature(raw_body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        return HttpResponseBadRequest("Invalid signature")

    payload = json.loads(raw_body)
    event_id = payload.get('payload', {}).get('payment', {}).get('entity', {}).get('id', '')
    event_type = payload.get('event', '')

    event, created = WebhookEvent.objects.get_or_create(
        event_id=event_id,
        gateway='razorpay',
        defaults={
            'event_type': event_type,
            'payload': payload,
            'raw_body': raw_body.decode('utf-8'),
        }
    )

    if not created and event.status == 'processed':
        return HttpResponse("Already processed", status=200)

    # Hand off to async immediately. Never process inline.
    process_webhook_event.delay(event.id)

    return HttpResponse("OK", status=200)
```

Note the `get_or_create` pattern. This is your application-level idempotency check before even hitting Celery. Return 200 for already-processed events because gateways interpret anything other than 200 as a failure and will retry.

---

## Async Processing: Why You Must Use Celery for Payments

I have seen payment reconciliation done synchronously in webhook handlers. It works until it does not. A slow database query, a downstream API call, or a transient network error causes the handler to time out. The gateway retries. You process twice.

Move all payment processing off the request-response cycle. Use Celery with Redis as the broker.

```python
# payments/tasks.py
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from .models import WebhookEvent, Payment


@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
)
def process_webhook_event(self, event_id: int):
    try:
        event = WebhookEvent.objects.select_for_update().get(id=event_id)

        if event.status == 'processed':
            return

        event.status = 'processing'
        event.save(update_fields=['status'])

        with transaction.atomic():
            _handle_event(event)

        event.status = 'processed'
        event.processed_at = timezone.now()
        event.save(update_fields=['status', 'processed_at'])

    except WebhookEvent.DoesNotExist:
        # Not worth retrying
        return
    except Exception as exc:
        event.status = 'failed'
        event.error_message = str(exc)
        event.save(update_fields=['status', 'error_message'])
        raise self.retry(exc=exc)


def _handle_event(event: WebhookEvent):
    if event.event_type == 'payment.captured':
        _handle_payment_captured(event)
    elif event.event_type == 'payment.failed':
        _handle_payment_failed(event)
    elif event.event_type == 'refund.processed':
        _handle_refund(event)


def _handle_payment_captured(event: WebhookEvent):
    gateway_payment_id = event.payload['payload']['payment']['entity']['id']
    amount = event.payload['payload']['payment']['entity']['amount']

    payment = Payment.objects.select_for_update().get(
        gateway_payment_id=gateway_payment_id
    )

    if payment.status == 'captured':
        return  # second line of defense against duplicates

    payment.status = 'captured'
    payment.save(update_fields=['status', 'updated_at'])

    # trigger downstream: fulfill order, send confirmation, etc.
    fulfill_order.delay(payment.order_id)
```

A few important details here:

`select_for_update()` on the Payment object prevents two concurrent Celery workers from processing the same payment simultaneously. This is the database-level lock.

`transaction.atomic()` wraps the entire business logic. If `fulfill_order.delay()` fails after updating `payment.status`, the whole thing rolls back and Celery retries from a clean state.

`retry_backoff=True` gives you exponential backoff: 60s, 120s, 240s, 480s, 600s. Payment failures are almost always transient (network hiccups, DB overload). You want retries, but not hammering every second.

---

## Reconciliation: The Part Nobody Builds

Webhooks are best-effort. They can fail silently, get lost in a network partition, or simply not arrive. You cannot rely on webhooks as your only source of truth.

Build a reconciliation job that runs on a schedule and cross-checks your database against the gateway's records.

```python
# payments/management/commands/reconcile_payments.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
import razorpay
from payments.models import Payment


class Command(BaseCommand):
    help = "Reconcile pending payments against Razorpay"

    def handle(self, *args, **options):
        client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )

        # Find payments that have been pending for more than 15 minutes
        cutoff = timezone.now() - timedelta(minutes=15)
        pending = Payment.objects.filter(
            status='pending',
            gateway='razorpay',
            created_at__lt=cutoff,
        ).select_for_update()

        reconciled = 0
        for payment in pending:
            try:
                gateway_payment = client.payment.fetch(payment.gateway_payment_id)
                gateway_status = gateway_payment['status']

                if gateway_status == 'captured' and payment.status != 'captured':
                    payment.status = 'captured'
                    payment.reconciled_at = timezone.now()
                    payment.save(update_fields=['status', 'reconciled_at'])
                    fulfill_order.delay(payment.order_id)
                    reconciled += 1

                elif gateway_status == 'failed' and payment.status != 'failed':
                    payment.status = 'failed'
                    payment.save(update_fields=['status'])

            except Exception as e:
                self.stderr.write(f"Failed to reconcile {payment.id}: {e}")
                continue

        self.stdout.write(f"Reconciled {reconciled} payments")
```

Run this with Celery Beat every 15 minutes:

```python
# celery.py (beat schedule)
CELERY_BEAT_SCHEDULE = {
    'reconcile-payments': {
        'task': 'payments.tasks.run_reconciliation',
        'schedule': 900,  # 15 minutes
    },
}
```

This job has caught real money in production. Webhooks that never arrived due to a server restart. Payments that were captured on the gateway side but the webhook handler crashed before writing to the DB. Without reconciliation, those transactions would have sat as "pending" forever and no one would have known.

---

## Production Checklist

Before any payment integration goes live, verify these:

| Check                                           | Why It Matters                                  |
| ----------------------------------------------- | ----------------------------------------------- |
| Webhook signature verification on every request | Prevents fake event injection                   |
| `unique_together` on `(event_id, gateway)`      | Database-level idempotency                      |
| `select_for_update()` on payment objects        | Prevents race conditions in concurrent workers  |
| `transaction.atomic()` around business logic    | Rollback on partial failure                     |
| Raw body stored before JSON parsing             | Required for accurate signature re-verification |
| Celery retry with exponential backoff           | Handles transient failures gracefully           |
| Reconciliation job running on schedule          | Catches missed webhooks                         |
| Return 200 for duplicate webhook events         | Prevents unnecessary gateway retries            |
| Separate Celery queues for payment processing   | Isolates payment workload from other tasks      |
| Alert on Celery retry exhaustion                | These are real money events, not log noise      |

---

## One Thing Most Tutorials Skip

The `raw_body` storage issue comes up constantly. When Django parses `request.body` as JSON and you later try to re-serialize it for signature verification, you get a different byte string. The HMAC will not match.

Always read `request.body` before any JSON parsing and store the original bytes. Do not decode, do not re-encode. Just store it.

This single issue has caused hours of debugging for developers I have worked with. The gateway signature check fails, the team thinks it is a secret key mismatch, they rotate keys, nothing changes. The bug was always the re-serialized body.

---

## Closing Thoughts

Payment integrations are not complex if you approach them with the right mental model. Treat every webhook as potentially duplicate. Treat every payment status update as a concurrency problem. Never trust webhooks alone for reconciliation.

The async architecture via Celery, the idempotency via `get_or_create` and `select_for_update`, and the reconciliation job are the three things that separate a payment integration that works in a demo from one that works in production at scale.

The code patterns here come from running PlusGold at Paytm and building the Synapse Conclave payment system. Both handled real money. Both are still running. The patterns hold up.
