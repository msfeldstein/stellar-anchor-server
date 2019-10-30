"""This module defines the asynchronous tasks needed for deposits, run via Celery."""
import logging

from celery.task.schedules import crontab
from celery.decorators import periodic_task
from django.conf import settings
from django.utils.timezone import now
from stellar_base.address import Address
from stellar_base.builder import Builder
from stellar_base.exceptions import HorizonError, StellarError
from stellar_base.horizon import Horizon
from stellar_base.keypair import Keypair

from app.celery import app
from transaction.models import Transaction

TRUSTLINE_FAILURE_XDR = "AAAAAAAAAGT/////AAAAAQAAAAAAAAAB////+gAAAAA="
SUCCESS_XDR = "AAAAAAAAAGQAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAA="

logger = logging.getLogger(__name__)

@app.task
def create_stellar_deposit(transaction_id):
    """Create and submit the Stellar transaction for the deposit."""
    transaction = Transaction.objects.get(id=transaction_id)

    # We check the Transaction status to avoid double submission of a Stellar
    # transaction. The Transaction can be either `pending_anchor` if the task
    # is called from `GET deposit/confirm_transaction` or `pending_trust` if called
    # from the `check_trustlines()`.
    if transaction.status not in [
        Transaction.STATUS.pending_anchor,
        Transaction.STATUS.pending_trust,
    ]:
        logger.debug(
            "unexpected transaction status %s at top of create_stellar_deposit",
            transaction.status,
        )
        return
    transaction.status = Transaction.STATUS.pending_stellar
    transaction.save()

    # We can assume transaction has valid stellar_account, amount_in, and asset
    # because this task is only called after those parameters are validated.
    payment_amount = round(transaction.amount_in - transaction.amount_fee, 7)
    
    # If the given Stellar account does not exist, create
    # the account with at least enough XLM for the minimum
    # reserve and a trust line (recommended 2.01 XLM), update
    # the transaction in our internal database, and return.
    address = Address(
        transaction.stellar_account,
        network=settings.STELLAR_NETWORK,
        horizon_uri=settings.HORIZON_URI,
    )
    # Check to see if the account exists. If so, do a normal payment.
    # If not, use the claimable payment mechanism
    try:
        address.get()
        filter_fn = lambda b: (b.get("asset_code", None) == transaction.asset.code and b.get("asset_issuer", None) == transaction.asset.issuer)
        print(address.balances)
        trustlines = list(filter(filter_fn, address.balances))
        if len(trustlines) == 0:
            print(">>>>> NO TRUST PAYMENT")
            perform_no_trust_payment(transaction, payment_amount)
        else:
            print(">>>> Standard payment")
            perform_standard_payment(transaction, payment_amount)
    except HorizonError as address_exc:
        # 404 code corresponds to Resource Missing.
        if address_exc.status_code != 404:
            # If this is not a 404, something unknown went wrong.
            logger.debug(
                "error with message %s when loading stellar account",
                address_exc.message,
            )
            raise address_exc
        print(">>>> New account payment")
        create_claimable_account(transaction, payment_amount)
        return
    

    

def perform_standard_payment(transaction, payment_amount):
    # If the account does exist, deposit the desired amount of the given
    # asset via a Stellar payment. If that payment succeeds, we update the
    # transaction to completed at the current time. If it fails due to a
    # trustline error, we update the database accordingly. Else, we do not update.

    builder = Builder(
        secret=settings.STELLAR_DISTRIBUTION_ACCOUNT_SEED,
        horizon_uri=settings.HORIZON_URI,
        network=settings.STELLAR_NETWORK,
    )
    builder.append_payment_op(
        destination=transaction.stellar_account,
        asset_code=transaction.asset.code,
        asset_issuer=transaction.asset.issuer,
        amount=str(payment_amount),
    )
    builder.sign()
    response = builder.submit()
    # If this condition is met, the Stellar payment succeeded, so we
    # can mark the transaction as completed.
    if response["result_xdr"] != SUCCESS_XDR:
        logger.debug("payment stellar transaction failed when submitted to horizon")
        return

    transaction.stellar_transaction_id = response["hash"]
    transaction.status = Transaction.STATUS.completed
    transaction.completed_at = now()
    transaction.status_eta = 0  # No more status change.
    transaction.amount_out = payment_amount
    transaction.save()

def perform_no_trust_payment(transaction, payment_amount):
    create_claimable_account(transaction, payment_amount, False)

def create_claimable_account(transaction, payment_amount, requires_account_creation = True):
    intermediate_account = Keypair.random()
    intermediate_key = intermediate_account.address().decode()
    print(">>>> Generated random account " + intermediate_key)
    starting_balance = settings.ACCOUNT_STARTING_BALANCE
    anchor_keypair = Keypair.from_seed(settings.STELLAR_DISTRIBUTION_ACCOUNT_SEED)
    anchor_address = anchor_keypair.address()
    print(f">>>> Distribution account {anchor_address}")
    builder = Builder(
        secret=settings.STELLAR_DISTRIBUTION_ACCOUNT_SEED,
        horizon_uri=settings.HORIZON_URI,
        network=settings.STELLAR_NETWORK,
    )
    print(">>>> create_account_op")
    builder.append_create_account_op(
        destination=intermediate_key,
        # TODO how do we programmatically figure out starting balance as base reserve can change
        # we should use requires_account_creation to tell how many ops we need to
        starting_balance="40",
        source=anchor_address,
    )
    print(">>>> change_trust")
    builder.append_change_trust_op(
        transaction.asset.code, transaction.asset.issuer,
        source=intermediate_key)
    print(">>>> payment")
    builder.append_payment_op(
        source=anchor_address,
        destination=intermediate_key,
        asset_code=transaction.asset.code,
        asset_issuer=transaction.asset.issuer,
        amount=str(payment_amount)
    )
    print(">>>> set_options")
    builder.append_set_options_op(
        source=intermediate_key,
        master_weight=0,
        signer_address=transaction.stellar_account,
        signer_weight=1,
        signer_type="ed25519PublicKey",
    )
    builder.sign()
    builder.sign(intermediate_account.seed())
    try:
        builder.submit()
    except HorizonError as submit_exc:
        print(">>>> Something went wrong!!!!! ")
        # op_src_no_trust means you don't have any assets or trustlines in the distribution account. Run create-stellar-token
        print(submit_exc)
        logger.debug(f"error with message {submit_exc.message} when submitting create account to horizon")
        return
    print(">>>>> Submitted intermediate account " + intermediate_key)
    transaction.stellar_transaction_id = response["hash"]
    transaction.status = Transaction.STATUS.completed
    transaction.completed_at = now()
    transaction.status_eta = 0  # No more status change.
    transaction.amount_out = payment_amount

@periodic_task(run_every=(crontab(minute="*/1")), ignore_result=True)
def check_trustlines():
    """
    Create Stellar transaction for deposit transactions marked as pending trust, if a
    trustline has been created.
    """
    transactions = Transaction.objects.filter(
        status=Transaction.STATUS.pending_trust)
    horizon = Horizon(horizon_uri=settings.HORIZON_URI)
    for transaction in transactions:
        try:
            account = horizon.account(transaction.stellar_account)
        except (StellarError, HorizonError) as exc:
            logger.debug("could not load account using provided horizon URI")
            continue
        try:
            balances = account["balances"]
        except KeyError:
            logger.debug("horizon account response had no balances")
            continue
        for balance in balances:
            try:
                asset_code = balance["asset_code"]
            except KeyError:
                logger.debug("horizon balances had no asset_code")
                continue
            if asset_code == transaction.asset.code:
                create_stellar_deposit(transaction.id)


if __name__ == "__main__":
    app.worker_main()
