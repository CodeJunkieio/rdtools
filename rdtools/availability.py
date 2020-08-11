"""
Functions for detecting and quantifying photovoltaic system downtime.
"""


import pandas as pd
import numpy as np


def is_online(inverters, meter, low_limit=None):
    """
    Detect offline inverters by comparing inverter power against meter power.

    This implementation is robust to inverter communications outages where all
    inverters appear to be offline but are actually producing as long as the
    meter data is unaffected.  In cases where some inverters are offline and
    others are merely not communicating, it will probably flag both offline and
    non-communicating inverters as offline.

    Parameters
    ----------
    inverters : pd.DataFrame
        Timeseries inverter power measurements
    meter : pd.Series
        Timeseries meter power measurements
    low_limit : float or pd.Series, optional
        The threshold to consider an inverter offline.  If a float is given,
        it is used for all inverters.  If a series is given, index values
        must match the column names in ``inverters``.  If not specified,
        ``0.01 * inverters.quantile(0.99)`` will be used.

    Returns
    -------
    online_mask : pd.DataFrame
        A dataframe with the same columns and index as ``inverters`` with
        boolean values representing inferred inverter status.  ``True`` means
        online and ``False`` means "may be offline".
    """
    times = inverters.index
    inverters = inverters.fillna(0)
    meter = meter.fillna(0)
    n_inv = inverters.shape[1]

    # inverter-specific threshold to determine online or offline
    if low_limit is None:
        low_limit = 0.01 * inverters.quantile(0.99)

    # detect inverter downtime based on the mean reported
    # inverter power.  this is more robust to cases where inverters
    # are online but not reporting data.
    mean_inverter_power = inverters[inverters > low_limit].mean(axis=1)

    # note that above line assumes that the mean reporting power is the same as
    # the true mean power -- not robust to inverters of vastly different sizes
    # with some not reporting.
    # apply correction for relative sizing based on who's reporting
    relative_sizing = inverters[inverters > low_limit] \
                               .divide(mean_inverter_power, axis=0) \
                               .median()
    mean_inverter_power = inverters[inverters > low_limit] \
                                   .divide(relative_sizing, axis=1) \
                                   .mean(axis=1)

    # if no inverters appear online, can't determine mean inverter power
    all_inverters_appear_offline = (inverters < low_limit).all(axis=1)
    mean_inverter_power[all_inverters_appear_offline] = meter / n_inv

    # if both meter and inverters look offline, we'll say it's offline
    meter_appears_offline = meter < np.sum(low_limit)
    site_offline = all_inverters_appear_offline & meter_appears_offline

    # calculate % diff between theoretical production if all invs were online
    # and actual meter readings
    meter_delta = 1 - meter / (n_inv * mean_inverter_power)

    # calculate the expected delta if the smallest inverter that LOOKS offline
    # actually IS offline:
    inverter_fraction = relative_sizing / relative_sizing.sum()
    smallest_delta = inverters.le(low_limit) \
                              .replace(False, np.nan) \
                              .multiply(inverter_fraction) \
                              .min(axis=1) \
                              .fillna(1)  # if nothing, use safe value of 100%
    meter_appears_low = meter_delta > (0.75 * smallest_delta)

    # if meter is low enough relative to what you would expect from inverters
    # that one might be offline, AND some actually look offline, assume there
    # are offline inverters
    inverters_appear_offline = ~(inverters > low_limit).all(axis=1)
    inverters_offline = inverters_appear_offline & meter_appears_low

    # assume that if at least 1 inv is offline then any inv below threshold is
    # offline.  this will overcount if some invs are online but not reporting.
    online_mask = pd.DataFrame(index=times,
                               columns=inverters.columns,
                               data=True)
    online_mask.loc[inverters_offline, :] = inverters.gt(low_limit)
    online_mask.loc[site_offline, :] = False
    return online_mask


def downtime_loss(inverters, meter, online_mask, expected_power,
                  production_profile, is_daylight, system_limit=None):
    """
    Determine lost production due to inverter downtime.

    Parameters
    ----------
    inverters : pd.DataFrame
        Timeseries inverter power measurements
    meter : pd.Series
        Timeseries meter power measurements
    online_mask : pd.DataFrame
        A boolean mask matching the columns and index of ``inverters``.
    expected_power : pd.Series
        Expected sitewide power, assuming all inverters were producing.  This
        is expected to be modeled from onsite measured weather conditions.
        Used for estimating lost production when all inverters are offline.
    production_profile : pd.DataFrame
        A 12 column by 24 row dataframe indicating typical hourly production.
        Used in place of ``expected_power`` when it is unavailable.
    is_daylight : pd.Series
        A boolean timeseries indicating whether weather conditions are
        sufficient to expect inverter production.  Since power measurements can
        be strange around sunrise and sunset, using a filter like
        ``solar_position['elevation'] > 5`` or similar can prevent spurious
        downtime at the edges of the day.
    system_limit : float, optional
        A sitewide power limit to use as a ceiling for (meter + lost_power).

    Returns
    -------
    lost_power : pd.Series
        Timeseries estimated lost power due to inverter downtime.
    """
    times = inverters.index
    inverters = inverters.fillna(0)
    meter = meter.fillna(0)
    expected_power = expected_power.fillna(0)

    # inverter-specific fraction of total production
    inverter_shares = inverters[online_mask].div(meter, axis=0).median()
    inverter_shares /= inverter_shares.sum()  # normalize it so sum == 1

    # timeseries fraction of online site capacity
    online_fraction = online_mask.multiply(inverter_shares, axis=1).sum(axis=1)

    # online_fraction will be too low during mixed comms and production outage.
    # fix this scaling by the ratio of meter to inv_sum (which is
    # bigger the more inverters are producing but not communicating).
    inv_sum = inverters.sum(axis=1)
    communications_factor = (meter / inv_sum).clip(lower=1)
    # if no invs are reporting power, can't calculate this comms ratio.
    # it could be either total system production outage or just bad comms
    # issues outage.  either way it doesn't make sense to apply the correction.
    communications_factor[online_fraction == 0] = 1
    online_fraction *= communications_factor
    online_fraction = online_fraction.clip(upper=1)

    # power that would have been produced if everything was online.
    # NOTE: this asumes that each inverter acts independently,
    # i.e. one inverter being offline doesn't affect the others.  This is not
    # necessarily true for systems with plant controllers, for instance.
    estimated_full_power = meter / online_fraction

    lost_power = estimated_full_power - meter
    lost_power = lost_power.clip(lower=0)

    # filter by solar elevation to avoid "downtime" on the edges of
    # the day when stuff gets unpredictable/inconsistent in low-light.
    lost_power.loc[~is_daylight] = 0

    # for the case where all inverters are offline, we fall back to using
    # site expected power (based on met data) or, if met data isn't
    # available either, fall back to the TMY monthly production profile.
    typical_power = profile_to_signal(production_profile, times)
    replacement_power = np.where(expected_power > 0,
                                 expected_power,
                                 typical_power)

    # hourly TMY profile is broadcasted to subhourly values, so need to ignore
    # times where the subhourly values don't count as daytime
    lost_power[(online_fraction == 0) & is_daylight] = replacement_power

    # don't let the total power (actual + lost) go above AC limit
    if system_limit:
        total_power = meter + lost_power
        total_power_clipped = total_power.clip(upper=system_limit)
        lost_power = total_power_clipped - meter
        lost_power = lost_power.clip(lower=0, upper=system_limit)

    return lost_power


def profile_to_signal(profile, times):
    """Convert a 12x24 to a timeseries"""
    aux = pd.DataFrame(index=times)
    aux['Hour'] = aux.index.hour
    aux['Month'] = aux.index.month

    profile = profile.copy()
    months = profile.columns
    profile['Hour'] = profile.index
    profile = profile.melt(id_vars=['Hour'], value_vars=months)
    profile['Month'] = profile['Month'].astype(int)

    signal = pd.merge(aux, profile, on=['Month', 'Hour'], how='left')
    signal.index = times
    return signal['value']


def signal_to_profile(signal):
    """Convert a timeseries to a 12x24"""
    aux = pd.DataFrame({'value': signal})
    aux['Hour'] = aux.index.hour
    aux['Month'] = aux.index.month
    profile = aux.pivot_table(values='value', index='Hour', columns='Month')
    return profile
